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

def _build_sos_task(entity_name: str, state_code: str,
                    name_type: str, first_name: str, last_name: str) -> str:
    sos = SOS_REGISTRY[state_code]
    search_name = re.sub(r'\s*\(.*?\)', '', entity_name).strip()

    # Per-state portal navigation guide (if available)
    portal_guide = PORTAL_INSTRUCTIONS.get(state_code, "")
    portal_section = ""
    if portal_guide:
        portal_section = (
            f"\n\n═══ {sos['name'].upper()} PORTAL NAVIGATION GUIDE ═══\n"
            f"{portal_guide}\n"
            f"═══ END PORTAL GUIDE ═══\n"
        )

    if name_type == "PERSON":
        return f"""Navigate to {sos['url']}

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
        return f"""Navigate to {sos['url']}

This is the {sos['name']} Secretary of State business entity search.
{portal_section}
The name "{search_name}" could be a business or a person. Try entity search first.
If no entity results, try officer/agent name search with First="{first_name}" Last="{last_name}".

Extract: entity name, status, type, formation date, DBA,
registered agent + address, and ALL officers with names, titles, and addresses.

Report exact data only. Do NOT fabricate."""

    else:
        return f"""Navigate to {sos['url']}

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
        )

    # Fallback: try final_result text
    raw = history.final_result() or ""
    return SOSResult(
        entity_name=entity_name, state=state_code,
        source_url=SOS_REGISTRY[state_code]["url"],
        confidence="LOW",
        raw_text=raw[:3000],
    )


# ── Single entity lookup ─────────────────────────────────────

async def _run_single_sos(
    entity_name: str,
    state_code: str,
    api_key: str,
    browser: Browser,
) -> SOSResult:
    """Run SOS lookup for one entity using a shared browser instance."""
    search_name = re.sub(r'\s*\(.*?\)', '', entity_name).strip()
    name_type, first_name, last_name = classify_name(search_name)
    logger.info(f"Name classification for '{search_name}': {name_type}")

    task = _build_sos_task(entity_name, state_code, name_type, first_name, last_name)
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
