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
) -> CompanyResult:
    """
    Search the web for company-level intelligence about a franchise entity.
    Uses Browser Use Cloud for managed browser automation.
    """
    start = time.time()
    key = api_key or BROWSER_USE_API_KEY

    context_parts = [f'"{entity_name}"']
    if franchisor:
        context_parts.append(f'"{franchisor}"')
    if state:
        context_parts.append(state)
    search_context = " ".join(context_parts)

    task = f"""Research the company "{entity_name}" for a sales intelligence report.
{f'This company is a franchisee of {franchisor}.' if franchisor else ''}
{f'They operate in {state}.' if state else ''}

Search Google for the following and visit the most relevant results:

1. Find the company's official website:
   Search: {search_context}

2. Search for recent news and articles:
   Search: {search_context} news
   Look for: expansions, closures, leadership changes, acquisitions,
   lawsuits, regulatory issues, awards, or recognition.
   Visit 2-3 relevant results and summarize.

3. Search for press releases:
   Search: {search_context} press release

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
