"""
OpenAI GPT-4o mini API helpers — direct API calls for extraction and structuring.
Cheap and fast, on par with Gemini Flash 2.5 for tabular parsing tasks.
Includes a global API key health tracker to fail-fast when the key is dead.
"""

import asyncio
import json
import logging
import re
import time

import httpx

from config import OPENAI_API_KEY, OPENAI_MODEL
from rate_limiter import get_rate_limiter

logger = logging.getLogger("fdd-agent")


# ── Global API Key Health Tracker ─────────────────────────────
_api_key_status: dict[str, dict] = {}


class APIKeyDeadError(Exception):
    """Raised when the API key is known to be invalid/suspended."""
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


def _check_fatal_error(status_code: int, body: str, api_key: str) -> bool:
    """Check for fatal (non-retryable) errors. Returns True if fatal."""
    if status_code == 401:
        mark_key_dead(api_key, f"HTTP 401 Unauthorized: {body[:200]}")
        return True
    if status_code == 403:
        mark_key_dead(api_key, f"HTTP 403 Forbidden: {body[:200]}")
        return True
    return False


# ── OpenAI Chat Completions API ──────────────────────────────

async def call_openai(
    system: str,
    user: str,
    api_key: str,
    model: str = "",
    max_tokens: int = 8192,
) -> str:
    """
    Call the OpenAI Chat Completions API directly via httpx.
    Defaults to GPT-4o mini for fast, cheap parsing.
    """
    check_key_alive(api_key)
    model = model or OPENAI_MODEL

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    max_retries = 5
    limiter = get_rate_limiter()

    for attempt in range(max_retries):
        check_key_alive(api_key)
        await limiter.acquire()

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, headers=headers, json=body)

            # Update rate limiter from OpenAI response headers
            limiter.update_from_headers(dict(resp.headers))

            if _check_fatal_error(resp.status_code, resp.text, api_key):
                raise APIKeyDeadError(f"API key error: {resp.text[:200]}")

            if resp.status_code == 429 and attempt < max_retries - 1:
                retry_after = resp.headers.get("retry-after")
                wait = int(retry_after) if retry_after else min(2 ** attempt * 2 + 1, 60)
                logger.warning(
                    f"OpenAI 429 — retrying in {wait}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code == 503 and attempt < max_retries - 1:
                wait = min(2 ** attempt * 3 + 2, 60)
                logger.warning(f"OpenAI 503 overloaded — retrying in {wait}s")
                await asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI API {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""

    raise RuntimeError("OpenAI API: max retries exceeded")


async def call_extraction(
    system: str,
    user: str,
    api_key: str,
    max_tokens: int = 16384,
) -> str:
    """Call GPT-4o mini for PDF extraction — fast and cheap, handles tabular parsing fine."""
    return await call_openai(
        system=system, user=user, api_key=api_key,
        model=OPENAI_MODEL, max_tokens=max_tokens,
    )


async def call_structure(
    prompt: str,
    system: str,
    api_key: str,
    max_tokens: int = 4096,
) -> str:
    """Call GPT-4o mini for lightweight structuring tasks (JSON parsing, data formatting)."""
    return await call_openai(
        system=system, user=prompt, api_key=api_key,
        model=OPENAI_MODEL, max_tokens=max_tokens,
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

    # Strip parenthetical qualifiers: "Ekstrom, Dennis (DTAZ, LLC)" → "Ekstrom, Dennis"
    cleaned = re.sub(r'\s*\(.*?\)', '', name).strip()

    # Check for business suffixes → definitely ENTITY
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

    # Simple 2-4 word name → likely a person
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
