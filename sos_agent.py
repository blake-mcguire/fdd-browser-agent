"""
SOS browser agent — searches Secretary of State portals for entity filings.
Uses open-source browser-use with a local Chromium browser and GPT-4o for reasoning.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

from pydantic import BaseModel
from browser_use import Agent, Browser, ChatGoogle

from config import (
    GOOGLE_API_KEY, SOS_REGISTRY, SOS_TIMEOUT, SOS_MAX_STEPS,
    SOS_INTER_ENTITY_DELAY, STATUTORY_AGENTS, BIZ_SUFFIXES, is_statutory,
    SOS_BROWSER_MODEL, BROWSER_HEADLESS, GLOBAL_BROWSER_CAP,
    SOS_VALIDATION_RETRIES, SOS_SUCCESS_CRITERIA, SOS_DEFAULT_CRITERIA,
)
from models import Officer, SOSResult, PersonEntry
from llm import classify_name, GeminiOverloadedError

# Fallback model for browser-use agent when primary is 503-ing
SOS_FALLBACK_MODEL = os.getenv("SOS_FALLBACK_MODEL", "gemini-2.5-flash-lite")
from sos_portal_instructions import PORTAL_INSTRUCTIONS

logger = logging.getLogger("fdd-agent")

_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "esq", "esq.", "phd", "md", "dds"}

# Global browser semaphore — caps total concurrent local browser instances
_global_browser_sem: asyncio.Semaphore | None = None

def _get_global_browser_sem() -> asyncio.Semaphore:
    global _global_browser_sem
    if _global_browser_sem is None:
        _global_browser_sem = asyncio.Semaphore(GLOBAL_BROWSER_CAP)
    return _global_browser_sem


# ── Structured output schema for browser-use ─────────────────

class SOSOfficer(BaseModel):
    name: str = ""
    title: str = ""
    address: str = ""

class SOSExtraction(BaseModel):
    registered_agent: str = ""
    agent_address: str = ""
    entity_status: str = ""
    formation_date: str = ""
    entity_type: str = ""
    dba_name: str = ""
    officers: list[SOSOfficer] = []
    confidence: str = "LOW"
    # Agent's own self-report — populated before finalizing. Forces the LLM to
    # literally state what it captured, which surfaces empty-officers cases
    # (e.g. UT clicking 'Associated DBAs' before gathering Principals).
    checkpoint: str = ""


# ── LLM + Browser setup ─────────────────────────────────────

def _build_llm(api_key: str, model: str = "") -> ChatGoogle:
    return ChatGoogle(
        model=model or SOS_BROWSER_MODEL,
        api_key=api_key,
        temperature=0,
        thinking_budget=0,     # disable thinking tokens for speed/cost
        max_retries=5,         # built-in 429 retry with backoff
        max_output_tokens=8096,
    )


def _build_fallback_llm(api_key: str) -> ChatGoogle | None:
    """Build a fallback LLM for browser-use to switch to on 503s."""
    if SOS_FALLBACK_MODEL and SOS_FALLBACK_MODEL != SOS_BROWSER_MODEL:
        return _build_llm(api_key, model=SOS_FALLBACK_MODEL)
    return None


def _new_browser() -> Browser:
    return Browser(
        headless=BROWSER_HEADLESS,
        viewport={"width": 1280, "height": 900},
    )


# ── Task prompt builder ──────────────────────────────────────

def _state_criteria(state_code: str) -> dict:
    return SOS_SUCCESS_CRITERIA.get(state_code, SOS_DEFAULT_CRITERIA)


def _build_checkpoint_directive(state_code: str) -> str:
    """Universal self-report + completion-gate appended to every SOS task."""
    crit = _state_criteria(state_code)
    req = ", ".join(crit["required_fields"])
    officers_rule = (
        "officers_count MUST be >= 1 UNLESS the detail page genuinely has no "
        "officer/principal/member/manager section visible (in which case set "
        "checkpoint note: 'no_officers_section_present')"
        if crit["require_officers"]
        else "officers_count may be 0 for this state"
    )
    return f"""

═══ COMPLETION GATE — READ BEFORE FINALIZING ═══
Before emitting your final structured output you MUST populate the
`checkpoint` field with a single line in EXACTLY this format:

CHECKPOINT: status=<value>, agent=<value>, agent_address=<value>, officers_count=<N>, dbas_count=<N>

Rules for this state ({state_code}):
- Required fields (must not be empty/UNKNOWN): {req}
- {officers_rule}

If ANY required field is still empty, OR officers_count=0 when officers are
required, DO NOT finalize. Go back to the detail page, scroll, expand any
collapsed panels (Officers / Principals / Members / Managers / Governors /
Parties / Governing Authorities / Additional Details / etc.), and re-extract
before writing the final output. Never navigate AWAY from the detail page
(e.g., to an 'Associated DBAs' or 'Filing History' sub-page) until all
required fields on the current page have been captured.

Do NOT declare success if the checkpoint values contradict the rules above.
═══ END COMPLETION GATE ═══
"""


def _retry_preamble(previous_failure: str, state_code: str) -> str:
    """Prepended to the task on a retry attempt — tells the agent what to fix."""
    if not previous_failure:
        return ""
    return f"""⚠ RETRY ATTEMPT — the previous run of this task failed validation.

FAILURE REASON FROM PREVIOUS ATTEMPT:
  {previous_failure}

This attempt MUST specifically fix the issue above. Common causes:
- Navigating away from the detail page before collecting all required fields.
- Missing a collapsed/expandable section (Principals, Additional Details,
  Parties, Managers, etc.) — expand EVERY collapsible section.
- Reading the page before an Ajax load finished — wait for the table or
  section to render before extracting.
- Clicking the wrong 'Details' / hyperlink on the results table — pick the
  exact or closest name match only.

Proceed with the full walkthrough below, but prioritize filling the field(s)
that were missing last time.

"""


def _build_sos_task(
    entity_name: str,
    state_code: str,
    name_type: str,
    first_name: str,
    last_name: str,
    previous_failure: str = "",
) -> str:
    sos = SOS_REGISTRY[state_code]
    search_name = re.sub(r'\s*\(.*?\)', '', entity_name).strip()

    # Per-state portal navigation guide (if available)
    portal_guide = PORTAL_INSTRUCTIONS.get(state_code, "")
    portal_section = ""
    if portal_guide:
        # Inject credentials for states that require login
        if state_code == "MI":
            from config import MI_SOS_USER, MI_SOS_PASS
            portal_guide = portal_guide.replace(
                "the MI_SOS_USER environment variable",
                f"'{MI_SOS_USER}'"
            ).replace(
                "the MI_SOS_PASS environment variable",
                f"'{MI_SOS_PASS}'"
            )
        portal_section = (
            f"\n\n═══ {sos['name'].upper()} PORTAL NAVIGATION GUIDE ═══\n"
            f"{portal_guide}\n"
            f"═══ END PORTAL GUIDE ═══\n"
        )

    retry_section = _retry_preamble(previous_failure, state_code)
    completion_gate = _build_checkpoint_directive(state_code)

    if name_type == "PERSON":
        body = f"""Navigate to {sos['url']}

This is the {sos['name']} Secretary of State business entity search.
{portal_section}
"{search_name}" is a PERSON's name, NOT a business entity name.

Search by OFFICER/AGENT name if available:
1. Look for an officer, agent, principal, or member name search option
2. If available, enter: First="{first_name}" Last="{last_name}"
3. If no officer search, try last name "{last_name}" as an entity name
4. For each business found, extract entity name, status, type, formation date,
   registered agent + address, and ALL officers with names, titles, and addresses.

Report exact data only. Do NOT fabricate."""

    elif name_type == "AMBIGUOUS":
        body = f"""Navigate to {sos['url']}

This is the {sos['name']} Secretary of State business entity search.
{portal_section}
The name "{search_name}" could be a business or a person. Try entity search first.
If no entity results, try officer/agent name search with First="{first_name}" Last="{last_name}".

Extract: entity name, status, type, formation date, DBA,
registered agent + address, and ALL officers with names, titles, and addresses.

Report exact data only. Do NOT fabricate."""

    else:
        body = f"""Navigate to {sos['url']}

This is the {sos['name']} Secretary of State business entity search.
{portal_section}
Search for: {search_name}
(Full name: "{entity_name}" — ignore text in parentheses)

Click the closest match. On the detail page, extract:
- Registered Agent: full name AND mailing address
- Entity Status (Active/Inactive/Dissolved)
- Formation/Registration Date
- Entity Type (LLC/Corporation/LP)
- DBA / Trade Name

CRITICAL — Officers/Members/Managers/Directors:
- Click EVERY tab on the detail page (Officers, Members, Annual Report, etc.)
- Many portals hide officer info behind separate tabs or links
- For EACH person: name, title/role, and their ADDRESS
- Do NOT stop after finding only the Registered Agent

If no results, retry without "Inc.", "LLC", etc., then try first 2-3 words only.
Report EXACTLY what the page shows. Do NOT invent data."""

    return retry_section + body + completion_gate


# ── Parse agent result into SOSResult ────────────────────────

def _parse_agent_result(
    history, entity_name: str, state_code: str,
) -> SOSResult:
    """Parse the AgentHistoryList into a SOSResult."""
    # Try structured output first
    data = history.get_structured_output(SOSExtraction)
    if data is not None:
        officers = [
            Officer(name=o.name, title=o.title or "UNKNOWN", address=o.address or "UNKNOWN")
            for o in (data.officers or []) if o.name and not is_statutory(o.name)
        ]
        # Checkpoint is stashed in raw_text so the validator can see self-reported
        # confirmations like 'no_officers_section_present' without a schema change
        # on the main SOSResult model.
        checkpoint = (data.checkpoint or "").strip()
        if checkpoint:
            logger.info(f"SOS checkpoint [{state_code}/{entity_name}]: {checkpoint[:200]}")
        return SOSResult(
            entity_name=entity_name, state=state_code,
            registered_agent=data.registered_agent or "UNKNOWN",
            agent_address=data.agent_address or "UNKNOWN",
            entity_status=data.entity_status or "UNKNOWN",
            formation_date=data.formation_date or "UNKNOWN",
            entity_type=data.entity_type or "UNKNOWN",
            dba_name=data.dba_name or "UNKNOWN",
            officers=officers,
            source_url=SOS_REGISTRY[state_code]["url"],
            confidence=data.confidence or "LOW",
            raw_text=checkpoint,
        )

    # Fallback: try final_result text
    raw = history.final_result() or ""
    return SOSResult(
        entity_name=entity_name, state=state_code,
        source_url=SOS_REGISTRY[state_code]["url"],
        confidence="LOW",
        raw_text=raw[:3000],
    )


# ── Result validation ────────────────────────────────────────

_EMPTY_VALUES = {"", "UNKNOWN", "NOT FOUND", "N/A", "NONE", "NOT AVAILABLE"}


def _is_populated(value: str) -> bool:
    return bool(value) and value.strip().upper() not in _EMPTY_VALUES


def _completeness_score(result: SOSResult) -> int:
    """
    Score how complete a SOSResult is. Used to keep the best result across
    retry attempts so a degraded retry can never overwrite a better first pass.
    """
    if result is None or result.confidence == "FAILED":
        return 0
    score = 0
    # Core identity fields — heavy weight
    if _is_populated(getattr(result, "entity_status", "") or ""):
        score += 10
    if _is_populated(getattr(result, "registered_agent", "") or ""):
        score += 10
    # Supporting fields
    for field in ("agent_address", "formation_date", "entity_type", "dba_name"):
        if _is_populated(getattr(result, field, "") or ""):
            score += 2
    # Each captured officer is worth more than any single supporting field
    score += 5 * len(result.officers or [])
    return score


def validate_sos_result(result: SOSResult) -> tuple[bool, str]:
    """
    Check the SOSResult against per-state success criteria.

    Returns (is_valid, failure_reason). A failure_reason is a short,
    human-readable string suitable for injecting into a retry prompt.
    """
    # Already-failed results don't get re-validated
    if result.confidence == "FAILED":
        return False, result.error or "Agent marked run as FAILED"

    criteria = _state_criteria(result.state)
    missing = []
    for field in criteria["required_fields"]:
        val = (getattr(result, field, "") or "").strip()
        if val.upper() in _EMPTY_VALUES:
            missing.append(field)

    if missing:
        return False, (
            f"Required field(s) missing or UNKNOWN on the detail page: "
            f"{', '.join(missing)}. Re-open the detail page, scroll, expand "
            f"any collapsed sections, and capture {', '.join(missing)} "
            f"before finalizing."
        )

    if criteria["require_officers"] and not result.officers:
        # Accept empty-officers if the agent's checkpoint explicitly confirmed
        # there was no officer section on the page. We infer this from the
        # raw_text if present.
        checkpoint_note = (result.raw_text or "").lower()
        if "no_officers_section_present" in checkpoint_note:
            return True, ""
        # When the registered agent is a known statutory service (CT Corp,
        # COGENCY, NRAI, Northwest…), the principals/members section is
        # routinely empty — the entity uses a third-party agent and lists no
        # internal officers on the SOS page. Retrying makes the agent wander
        # and usually corrupts the otherwise-good capture.
        if is_statutory(result.registered_agent or ""):
            return True, ""
        return False, (
            "No officers/principals/members/managers were captured, but this "
            "state typically exposes them on the detail page. Scroll to the "
            "Principal/Officers/Members/Managers/Governors/Parties section, "
            "expand any collapsed panels or drop-downs, and capture every "
            "row. Only mark the section as empty if it is visibly empty."
        )

    return True, ""


# ── Single entity lookup ─────────────────────────────────────

async def _run_sos_once(
    entity_name: str,
    state_code: str,
    api_key: str,
    browser: Browser,
    previous_failure: str = "",
) -> SOSResult:
    """Single agent run — no retry logic. Returns a SOSResult (possibly invalid)."""
    search_name = re.sub(r'\s*\(.*?\)', '', entity_name).strip()
    name_type, first_name, last_name = classify_name(search_name)
    logger.info(f"Name classification for '{search_name}': {name_type}")

    task = _build_sos_task(
        entity_name, state_code, name_type, first_name, last_name,
        previous_failure=previous_failure,
    )
    llm = _build_llm(api_key)
    fallback = _build_fallback_llm(api_key)

    try:
        agent_kwargs = dict(
            task=task,
            llm=llm,
            browser=browser,
            output_model_schema=SOSExtraction,
            use_vision=True,
            max_failures=5,
        )
        if fallback:
            agent_kwargs["fallback_llm"] = fallback
        agent = Agent(**agent_kwargs)

        history = await asyncio.wait_for(
            agent.run(max_steps=SOS_MAX_STEPS),
            timeout=SOS_TIMEOUT,
        )

        return _parse_agent_result(history, entity_name, state_code)

    except asyncio.TimeoutError:
        return SOSResult(
            entity_name=entity_name, state=state_code,
            source_url=SOS_REGISTRY[state_code]["url"],
            confidence="FAILED", error=f"Timeout after {SOS_TIMEOUT}s",
        )
    except Exception as e:
        logger.error(f"SOS failed: {entity_name}/{state_code}: {e}")
        return SOSResult(
            entity_name=entity_name, state=state_code,
            source_url=SOS_REGISTRY[state_code]["url"],
            confidence="FAILED", error=str(e)[:500],
        )


async def _run_single_sos(
    entity_name: str,
    state_code: str,
    api_key: str,
    browser: Browser,
) -> SOSResult:
    """
    Run SOS lookup with validation + retry.

    First attempt runs cleanly. If the result fails per-state validation
    (e.g., missing required fields, no officers on a state that requires
    them), we retry up to SOS_VALIDATION_RETRIES more times with the
    failure reason injected into the task prompt so the agent can
    specifically address what went wrong.
    """
    previous_failure = ""
    best: SOSResult | None = None
    best_score = -1

    for attempt in range(1 + SOS_VALIDATION_RETRIES):
        if attempt > 0:
            logger.warning(
                f"SOS retry #{attempt} for {entity_name}/{state_code} — "
                f"reason: {previous_failure[:120]}"
            )

        result = await _run_sos_once(
            entity_name, state_code, api_key, browser,
            previous_failure=previous_failure,
        )

        score = _completeness_score(result)
        if score > best_score:
            best, best_score = result, score

        is_valid, reason = validate_sos_result(result)
        if is_valid:
            if attempt > 0:
                logger.info(
                    f"SOS retry succeeded for {entity_name}/{state_code} "
                    f"after {attempt} retry attempt(s)"
                )
            return result

        previous_failure = reason
        # If the agent itself errored out (timeout, 503, browser crash),
        # no point in retrying with a "fix your extraction" prompt — bail
        # with whatever the best prior attempt was (if any).
        if result.confidence == "FAILED":
            return best if best_score > 0 else result

    # All retries exhausted — return the BEST result seen across attempts
    # with a degraded confidence and an explanatory error.
    if best is not None:
        best.confidence = "LOW"
        suffix = f"Validation failed after {SOS_VALIDATION_RETRIES + 1} attempt(s): {previous_failure}"
        best.error = (best.error + " | " if best.error else "") + suffix
        logger.warning(
            f"SOS validation gave up for {entity_name}/{state_code} "
            f"(best_score={best_score}): {previous_failure[:200]}"
        )
        return best

    # Safety net (should not hit)
    return SOSResult(
        entity_name=entity_name, state=state_code,
        source_url=SOS_REGISTRY[state_code]["url"],
        confidence="FAILED",
        error="No result produced after retry loop",
    )


# ── Single entity lookup (standalone) ────────────────────────

async def sos_lookup(entity_name: str, state: str, api_key: str) -> SOSResult:
    """Run a single SOS lookup (creates its own browser)."""
    states = [s.strip().upper() for s in state.split(",") if s.strip()]
    primary = states[0] if states else ""

    if primary not in SOS_REGISTRY:
        return SOSResult(
            entity_name=entity_name, state=primary,
            confidence="FAILED", error=f"Unknown state: {primary}",
        )

    key = api_key or GOOGLE_API_KEY
    browser = _new_browser()

    try:
        result = await _run_single_sos(entity_name, primary, key, browser)
        return result
    except Exception as e:
        logger.error(f"SOS lookup failed: {entity_name}: {e}")
        return SOSResult(
            entity_name=entity_name, state=primary,
            confidence="FAILED", error=str(e)[:500],
        )
    finally:
        await browser.stop()


# ── Batch lookup (sub-batched with separate browsers) ────────

MAX_BATCH_SIZE = 8  # Max entities per browser session


async def sos_lookup_batch(
    entities: list[dict],
    state: str,
    api_key: str,
    on_result=None,
    on_start=None,
) -> list[SOSResult]:
    """
    Batch SOS lookup — splits large batches into sub-batches of MAX_BATCH_SIZE,
    each with its own local browser instance. If one browser crashes, only that
    sub-batch is affected.
    on_result: optional async callback(entity_dict, sos_result) called after each entity.
    on_start: optional async callback(entity_dict) called before each entity starts.
    """
    primary = state.strip().upper()
    key = api_key or GOOGLE_API_KEY

    if primary not in SOS_REGISTRY or not key:
        results = []
        for e in entities:
            r = SOSResult(
                entity_name=e.get("entity_name", "?"), state=primary,
                confidence="FAILED",
                error=f"Unknown state: {primary}" if primary not in SOS_REGISTRY else "No API key",
            )
            results.append(r)
            if on_result:
                await on_result(e, r)
        return results

    # Split into sub-batches
    sub_batches = []
    for i in range(0, len(entities), MAX_BATCH_SIZE):
        sub_batches.append(entities[i:i + MAX_BATCH_SIZE])

    logger.info(
        f"SOS batch {primary}: {len(entities)} entities → "
        f"{len(sub_batches)} sub-batches of ≤{MAX_BATCH_SIZE}"
    )

    all_results = []
    consecutive_503_failures = 0
    MAX_CONSECUTIVE_503 = 3  # abort batch after this many consecutive 503/overload failures

    for batch_idx, sub_batch in enumerate(sub_batches):
        logger.info(
            f"SOS sub-batch {batch_idx + 1}/{len(sub_batches)} for {primary}: "
            f"{len(sub_batch)} entities"
        )

        for i, entity_dict in enumerate(sub_batch):
            entity_name = entity_dict.get("entity_name", "?")
            logger.info(
                f"SOS batch {primary}: entity {batch_idx * MAX_BATCH_SIZE + i + 1}"
                f"/{len(entities)} — {entity_name}"
            )

            if on_start:
                await on_start(entity_dict)

            # Fresh browser per entity — browser-use agents don't reliably
            # reuse a browser left on a previous entity's SOS detail page.
            # Acquire global browser semaphore to cap total concurrent browsers.
            sem = _get_global_browser_sem()
            await sem.acquire()
            browser = _new_browser()
            try:
                sos_result = await _run_single_sos(
                    entity_name, primary, key, browser,
                )
            except Exception as e:
                logger.error(
                    f"SOS entity {entity_name}/{primary} failed: {e}"
                )
                sos_result = SOSResult(
                    entity_name=entity_name, state=primary,
                    confidence="FAILED",
                    error=f"Browser error: {str(e)[:200]}",
                )
            finally:
                try:
                    await browser.stop()
                except Exception:
                    pass
                sem.release()

            all_results.append(sos_result)

            if on_result:
                await on_result(entity_dict, sos_result)

            # Detect consecutive 503/overload failures — abort batch if Gemini is down
            err_text = (sos_result.error or "").lower()
            is_503_fail = (
                sos_result.confidence == "FAILED"
                and ("503" in err_text or "unavailable" in err_text
                     or "consecutive failures" in err_text
                     or "high demand" in err_text)
            )
            if is_503_fail:
                consecutive_503_failures += 1
                if consecutive_503_failures >= MAX_CONSECUTIVE_503:
                    logger.error(
                        f"SOS batch {primary}: {consecutive_503_failures} consecutive "
                        f"503/overload failures — aborting. Gemini API appears down."
                    )
                    raise GeminiOverloadedError(
                        f"Gemini API is overloaded — {consecutive_503_failures} consecutive "
                        f"SOS lookups failed with 503 errors. Please try again later."
                    )
            else:
                consecutive_503_failures = 0

            # Rate-limit delay between entities to avoid LLM API burst limits
            if SOS_INTER_ENTITY_DELAY > 0 and i < len(sub_batch) - 1:
                await asyncio.sleep(SOS_INTER_ENTITY_DELAY)

    return all_results


# ── People extraction from SOS result ────────────────────────

def build_people_list(sos_result: SOSResult) -> list[PersonEntry]:
    """Extract all human officers + non-statutory registered agent from SOS result."""
    people = []
    seen_names = set()

    for o in sos_result.officers:
        name = o.name.strip()
        if not name or name.upper() == "UNKNOWN":
            continue
        if is_statutory(name):
            continue
        name_tokens = name.upper().split()
        if name_tokens and name_tokens[-1] in BIZ_SUFFIXES:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        first, last = _split_name(name)
        people.append(PersonEntry(
            name=name, title=o.title, address=o.address,
            first_name=first, last_name=last,
        ))

    agent_name = sos_result.registered_agent.strip()
    if agent_name and agent_name.upper() not in ("UNKNOWN", "NOT FOUND", "N/A", "NONE", ""):
        if not is_statutory(agent_name):
            name_tokens = agent_name.upper().split()
            is_biz = name_tokens and name_tokens[-1] in BIZ_SUFFIXES
            key = agent_name.lower()
            if key not in seen_names and not is_biz:
                seen_names.add(key)
                first, last = _split_name(agent_name)
                people.append(PersonEntry(
                    name=agent_name, title="Registered Agent",
                    address=sos_result.agent_address,
                    first_name=first, last_name=last,
                ))

    return people


def _split_name(name: str) -> tuple[str, str]:
    raw = name.replace(",", " ")
    parts = raw.split()
    while len(parts) > 1 and parts[-1].lower().rstrip(".") in {s.rstrip(".") for s in _SUFFIXES}:
        parts.pop()
    first = parts[0] if parts else name
    last = parts[-1] if len(parts) > 1 else ""
    return first, last
