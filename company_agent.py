"""
Company enrichment cloud agent.
Searches the web for company intelligence: news, website, expansions, closures, etc.
Uses Browser Use Cloud SDK v2 for managed browser sessions.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from pydantic import BaseModel
from browser_use_sdk import AsyncBrowserUse

from config import BROWSER_USE_API_KEY, BROWSER_USE_MODEL, COMPANY_TIMEOUT, COMPANY_MAX_STEPS
from models import CompanyResult

logger = logging.getLogger("fdd-agent")


# ── Structured output schema ─────────────────────────────────

class CompanyExtraction(BaseModel):
    website: Optional[str] = ""
    recent_news_summary: Optional[str] = ""
    key_developments: list[str] = []
    risk_signals: list[str] = []
    notes: Optional[str] = ""


async def company_enrichment(
    entity_name: str,
    state: str,
    franchisor: str,
    api_key: str,
    user_context: str = "",
) -> CompanyResult:
    """
    Search the web for company-level intelligence about a franchise entity.
    Uses Browser Use Cloud for managed browser automation.

    `user_context` is free-text context provided at upload time (e.g.
    "This is a Del Taco FDD list"). It's prepended to the task prompt
    so the agent can use the information to disambiguate search results
    and focus on the right company.
    """
    start = time.time()
    key = api_key or BROWSER_USE_API_KEY

    context_parts = [f'"{entity_name}"']
    if franchisor:
        context_parts.append(f'"{franchisor}"')
    if state:
        context_parts.append(state)
    search_context = " ".join(context_parts)

    has_context = bool((user_context or "").strip())
    user_context_block = ""
    if has_context:
        user_context_block = f"""═══ USER-PROVIDED LIST CONTEXT ═══
{user_context.strip()}

TREAT THIS CONTEXT AS AUTHORITATIVE for every entity in this batch.
Two concrete ways you MUST use it:

1) SEARCH QUERY CONSTRUCTION — when building Google searches, include
   the BRAND / FRANCHISE / PARENT-COMPANY name from the context above
   alongside the entity name. The shell LLC ("{entity_name}") is often
   invisible online; the brand name is what surfaces the right results.

   Example — if the context says "This is a Del Taco FDD list", your
   queries should look like:
     '"{entity_name}" Del Taco'
     '"{entity_name}" Del Taco franchisee news'
     'Del Taco franchise {state} owner'
   BEFORE you fall back to the shell-LLC-only query.

2) RESULT DISAMBIGUATION — when multiple companies share a name,
   pick the one that fits the list context. Filter news/press to
   what is actually relevant to the list's domain.
═══ END LIST CONTEXT ═══

"""

    # Build search-query recipes. When user context is provided we
    # prepend context-brand queries because they vastly out-recall
    # shell-LLC queries for franchise entities.
    if has_context:
        site_query = (f"1. Find the company's official website:\n"
                      f"   PRIMARY: '\"{entity_name}\" <brand-from-context>'\n"
                      f"   FALLBACK: {search_context}\n"
                      f"   (Replace <brand-from-context> with the franchise/parent-company name\n"
                      f"   extracted from the LIST CONTEXT block above.)")
        news_query = (f"2. Search for recent news and articles:\n"
                      f"   PRIMARY: '\"{entity_name}\" <brand-from-context> news'\n"
                      f"   FALLBACK: '{search_context} news'\n"
                      f"   Look for: expansions, closures, leadership changes, acquisitions,\n"
                      f"   lawsuits, regulatory issues, awards, or recognition.\n"
                      f"   Visit 2-3 relevant results and summarize.")
        pr_query = (f"3. Search for press releases:\n"
                    f"   PRIMARY: '\"{entity_name}\" <brand-from-context> press release'\n"
                    f"   FALLBACK: '{search_context} press release'")
    else:
        site_query = (f"1. Find the company's official website:\n"
                      f"   Search: {search_context}")
        news_query = (f"2. Search for recent news and articles:\n"
                      f"   Search: {search_context} news\n"
                      f"   Look for: expansions, closures, leadership changes, acquisitions,\n"
                      f"   lawsuits, regulatory issues, awards, or recognition.\n"
                      f"   Visit 2-3 relevant results and summarize.")
        pr_query = (f"3. Search for press releases:\n"
                    f"   Search: {search_context} press release")

    task = f"""{user_context_block}Research the company "{entity_name}" for a sales intelligence report.
{f'This company is a franchisee of {franchisor}.' if franchisor else ''}
{f'They operate in {state}.' if state else ''}

Search Google for the following and visit the most relevant results:

{site_query}

{news_query}

{pr_query}

Compile your findings. Only report what you actually find — do NOT fabricate.
Focus on the last 2-3 years. If nothing found, say "No information found."
"""

    try:
        client = AsyncBrowserUse(api_key=key)
        task_handle = await asyncio.wait_for(
            client.tasks.create_task(
                task=task,
                llm=BROWSER_USE_MODEL,
                schema=CompanyExtraction,
                max_steps=COMPANY_MAX_STEPS,
            ),
            timeout=30,
        )

        result = await asyncio.wait_for(
            task_handle.complete(),
            timeout=COMPANY_TIMEOUT,
        )

        data = result.parsed_output
        elapsed = round(time.time() - start, 1)
        logger.info(f"Company enrichment for '{entity_name}' completed in {elapsed}s")

        if data is None:
            return CompanyResult(entity_name=entity_name, notes="No structured output returned")

        return CompanyResult(
            entity_name=entity_name,
            website=data.website or "",
            recent_news_summary=data.recent_news_summary or "",
            key_developments=data.key_developments or [],
            risk_signals=data.risk_signals or [],
            notes=data.notes or "",
        )

    except asyncio.TimeoutError:
        logger.warning(f"Company enrichment timeout for '{entity_name}'")
        return CompanyResult(
            entity_name=entity_name,
            error=f"Timeout after {COMPANY_TIMEOUT}s",
        )
    except Exception as e:
        logger.error(f"Company enrichment failed for '{entity_name}': {e}")
        return CompanyResult(
            entity_name=entity_name,
            error=str(e)[:500],
        )
