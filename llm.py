"""
Google Gemini API helpers — direct SDK calls for PDF extraction and JSON structuring.
Gemini 2.5 Flash: fast, cheap, excellent at tabular parsing.
Includes a global API key health tracker to fail-fast when the key is dead.
"""

import asyncio
import json
import logging
import re
import time

from google import genai
from google.genai import types

from config import GOOGLE_API_KEY, GEMINI_MODEL

# Fallback model if primary is overloaded (503).
# gemini-2.0-flash is EOL — use 2.5-flash-lite (lighter variant, less demand).
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash-lite"

logger = logging.getLogger("fdd-agent")


# ── Global API Key Health Tracker ─────────────────────────────
_api_key_status: dict[str, dict] = {}


class APIKeyDeadError(Exception):
    """Raised when the API key is known to be invalid/suspended."""
    pass


class GeminiOverloadedError(Exception):
    """Raised when Gemini API is persistently overloaded (503) across all models."""
    pass


class GeminiRateLimitError(Exception):
    """Raised when Gemini API rate limits (429) are exhausted after all retries."""
    pass


def mark_key_dead(api_key: str, reason: str):
    masked = api_key[:8] + "..." if len(api_key) > 8 else api_key
    if api_key not in _api_key_status or not _api_key_status[api_key].get("dead"):
        logger.error(f"API KEY DEAD — marking {masked} as unusable: {reason}")
    _api_key_status[api_key] = {"dead": True, "reason": reason, "since": time.time()}


def is_key_dead(api_key: str) -> bool:
    status = _api_key_status.get(api_key)
    if not status or not status.get("dead"):
        return False
    if time.time() - status["since"] > 300:
        logger.info("API key cooldown expired — allowing retry")
        _api_key_status[api_key]["dead"] = False
        return False
    return True


def check_key_alive(api_key: str):
    if is_key_dead(api_key):
        reason = _api_key_status[api_key].get("reason", "unknown")
        raise APIKeyDeadError(f"API key is suspended: {reason}")


# ── Gemini Client Cache ─────────────────────────────────────
_clients: dict[str, genai.Client] = {}


def _get_client(api_key: str) -> genai.Client:
    """Get or create a Gemini client for the given API key."""
    if api_key not in _clients:
        _clients[api_key] = genai.Client(api_key=api_key)
    return _clients[api_key]


# ── Gemini Chat Completions ─────────────────────────────────

async def call_gemini(
    system: str,
    user: str,
    api_key: str,
    model: str = "",
    max_tokens: int = 8192,
) -> str:
    """
    Call the Gemini API via the google-genai SDK.
    Defaults to Gemini 2.5 Flash for fast, cheap parsing.
    """
    check_key_alive(api_key)
    active_model = model or GEMINI_MODEL
    original_model = active_model

    max_retries = 7
    consecutive_503 = 0
    fell_back = False
    last_error = ""
    total_503 = 0
    total_429 = 0

    for attempt in range(max_retries):
        check_key_alive(api_key)

        try:
            client = _get_client(api_key)

            # Run the synchronous SDK call in a thread to avoid blocking asyncio
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=active_model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    temperature=0,
                ),
            )

            if response.text:
                return response.text
            return ""

        except (APIKeyDeadError, GeminiOverloadedError, GeminiRateLimitError):
            raise  # don't wrap our own exceptions

        except Exception as e:
            err_str = str(e)
            last_error = err_str

            # Check for fatal errors
            if "401" in err_str or "UNAUTHENTICATED" in err_str:
                mark_key_dead(api_key, f"Authentication failed: {err_str[:200]}")
                raise APIKeyDeadError(f"Gemini API key error: {err_str[:200]}")

            if "403" in err_str or "PERMISSION_DENIED" in err_str:
                mark_key_dead(api_key, f"Permission denied: {err_str[:200]}")
                raise APIKeyDeadError(f"Gemini API key error: {err_str[:200]}")

            # 404 — model doesn't exist. If on fallback already, fatal.
            # If on primary, switch to fallback immediately.
            if "404" in err_str or "NOT_FOUND" in err_str:
                if active_model == GEMINI_FALLBACK_MODEL:
                    raise RuntimeError(
                        f"Gemini model '{active_model}' not found (404). "
                        f"Check GEMINI_MODEL in your .env — the model may have been deprecated."
                    )
                logger.warning(
                    f"Gemini model '{active_model}' not found (404) — "
                    f"switching to {GEMINI_FALLBACK_MODEL}"
                )
                active_model = GEMINI_FALLBACK_MODEL
                fell_back = True
                continue

            # Classify the error
            is_503 = "503" in err_str or "UNAVAILABLE" in err_str
            is_429 = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
            is_retryable = is_503 or is_429 or "500" in err_str

            if is_503:
                total_503 += 1
                consecutive_503 += 1
                if consecutive_503 >= 3 and active_model != GEMINI_FALLBACK_MODEL:
                    logger.warning(
                        f"Gemini {active_model} overloaded ({consecutive_503}x 503) "
                        f"— falling back to {GEMINI_FALLBACK_MODEL}"
                    )
                    active_model = GEMINI_FALLBACK_MODEL
                    fell_back = True
                    consecutive_503 = 0
            else:
                consecutive_503 = 0

            if is_429:
                total_429 += 1

            if is_retryable and attempt < max_retries - 1:
                # 503 gets longer waits (server overload needs more cooldown)
                if is_503:
                    wait = min(2 ** attempt * 5 + 5, 90)
                else:
                    wait = min(2 ** attempt * 2 + 1, 60)
                logger.warning(
                    f"Gemini API error ({active_model}) — retrying in {wait}s "
                    f"(attempt {attempt + 1}/{max_retries}): {err_str[:120]}"
                )
                await asyncio.sleep(wait)
                continue

            # Retries exhausted — raise a typed exception
            if attempt >= max_retries - 1:
                if total_503 > 0:
                    models_tried = original_model
                    if fell_back:
                        models_tried += f" and {GEMINI_FALLBACK_MODEL}"
                    raise GeminiOverloadedError(
                        f"Gemini API is experiencing high demand. "
                        f"Tried {models_tried} — got {total_503} overload errors "
                        f"across {max_retries} attempts. Please try again in a few minutes."
                    )
                if total_429 > 0:
                    raise GeminiRateLimitError(
                        f"Gemini API rate limit exceeded. "
                        f"Got {total_429} rate-limit errors across {max_retries} attempts. "
                        f"Your account may need a higher tier, or try again after a brief wait."
                    )
                raise RuntimeError(
                    f"Gemini API failed after {max_retries} retries: {err_str[:300]}"
                )

            # Unknown error — retry with backoff
            wait = min(2 ** attempt * 2 + 1, 60)
            logger.warning(f"Gemini API unexpected error — retrying in {wait}s: {err_str[:100]}")
            await asyncio.sleep(wait)

    raise RuntimeError("Gemini API: max retries exceeded")


async def call_extraction(
    system: str,
    user: str,
    api_key: str,
    max_tokens: int = 16384,
) -> str:
    """Call Gemini for PDF extraction — fast and cheap, handles tabular parsing fine."""
    return await call_gemini(
        system=system, user=user, api_key=api_key,
        model=GEMINI_MODEL, max_tokens=max_tokens,
    )


async def call_structure(
    prompt: str,
    system: str,
    api_key: str,
    max_tokens: int = 4096,
) -> str:
    """Call Gemini for lightweight structuring tasks (JSON parsing, data formatting)."""
    return await call_gemini(
        system=system, user=prompt, api_key=api_key,
        model=GEMINI_MODEL, max_tokens=max_tokens,
    )


# ── JSON Parsing ─────────────────────────────────────────────

def parse_json_from_text(text: str) -> dict:
    """Extract the first JSON object from LLM output."""
    cleaned = re.sub(r"```json\s*", "", text)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()
    i, j = cleaned.find("{"), cleaned.rfind("}")
    if i != -1 and j > i:
        return json.loads(cleaned[i:j + 1])
    return {}


# ── Name Classification (heuristic — no API call) ────────────

_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "esq", "phd", "md", "dds"}


def classify_name(name: str, api_key: str = "") -> tuple[str, str, str]:
    """
    Classify whether `name` is a business entity or a person using heuristics.
    No API call needed — saves 1 request per entity.
    Returns (name_type, first_name, last_name) where name_type is ENTITY | PERSON | AMBIGUOUS.
    """
    from config import BIZ_SUFFIXES

    # Strip parenthetical qualifiers: "Ekstrom, Dennis (DTAZ, LLC)" -> "Ekstrom, Dennis"
    cleaned = re.sub(r'\s*\(.*?\)', '', name).strip()

    # Check for business suffixes -> definitely ENTITY
    tokens = cleaned.upper().replace(",", " ").split()
    for tok in tokens:
        if tok.rstrip(".") in {s.rstrip(".") for s in BIZ_SUFFIXES}:
            return "ENTITY", "", ""

    # Check for common business keywords not in BIZ_SUFFIXES
    lower = cleaned.lower()
    biz_keywords = [
        "restaurant", "restaurants", "food", "foods", "franchise", "franchising",
        "management", "consulting", "properties", "investments", "capital",
        "ventures", "development", "trust", "foundation", "fund",
        "international", "national", "american", "global",
    ]
    if any(kw in lower for kw in biz_keywords):
        return "ENTITY", "", ""

    # Handle "Last, First" format (common in FDD tables)
    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",", 1)]
        if len(parts) == 2 and all(1 <= len(p.split()) <= 2 for p in parts):
            last_part = parts[0]
            first_part = parts[1]
            return "PERSON", first_part, last_part

    # Simple 2-4 word name -> likely a person
    words = cleaned.split()
    if 2 <= len(words) <= 4 and all(
        w[0].isupper() and (w.rstrip(".").isalpha() or len(w) <= 3)
        for w in words if len(w) > 1
    ):
        core = [w for w in words if w.lower().rstrip(".") not in _NAME_SUFFIXES and len(w) > 2]
        if not core:
            core = words
        return "PERSON", core[0], core[-1] if len(core) > 1 else words[-1]

    if len(words) == 1:
        return "AMBIGUOUS", cleaned, ""

    if len(words) > 3:
        return "ENTITY", "", ""

    return "AMBIGUOUS", words[0] if words else cleaned, words[-1] if len(words) > 1 else ""
