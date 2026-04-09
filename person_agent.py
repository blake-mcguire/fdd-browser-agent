"""
Person search cloud agent — LinkedIn-focused progressive enrichment.

Strategy: Find the person's LinkedIn profile and gather professional info.
Contact details (phone, email, address) are captured only if they appear
organically during the search — never explicitly searched for.

The LinkedIn location (city/metro) is the key output for downstream
enrichment via data providers (Enformion, Apollo, etc.).
"""

import asyncio
import json
import logging
import time
from typing import Optional

from pydantic import BaseModel
from browser_use_sdk import AsyncBrowserUse

from config import BROWSER_USE_API_KEY, BROWSER_USE_MODEL, PERSON_TIMEOUT, PERSON_MAX_STEPS
from models import PersonResult, PersonEntry

logger = logging.getLogger("fdd-agent")


# ── Structured output schema ─────────────────────────────────

class PersonExtraction(BaseModel):
    linkedin_url: Optional[str] = ""
    linkedin_location: Optional[str] = ""       # e.g. "Scottsdale, Arizona" — critical for enrichment
    linkedin_headline: Optional[str] = ""       # e.g. "Franchise Owner at Desert Taco LLC"
    current_title: Optional[str] = ""
    background: Optional[str] = ""              # 2-3 sentence professional summary
    years_with_org: Optional[str] = ""
    education: Optional[str] = ""
    # Organic discoveries — captured if found, NOT actively searched for
    email: Optional[str] = ""
    phone: Optional[str] = ""
    personal_website: Optional[str] = ""


async def person_search(
    person: PersonEntry,
    entity_name: str,
    state: str,
    api_key: str,
) -> PersonResult:
    """
    Research a person's professional background, focused on LinkedIn resolution.

    The primary goal is to find their LinkedIn profile, which provides:
    - Location (city-level) for downstream enrichment matching
    - Professional background for sales outreach context
    - Any contact info that appears naturally

    Contact info is NOT explicitly searched for — it's captured only
    if it shows up organically in search results or profile pages.
    """
    start = time.time()
    key = api_key or BROWSER_USE_API_KEY

    person_name = person.name
    title = person.title
    address = person.address

    task = f"""Research the professional background of "{person_name}" who is
the {title} of "{entity_name}" in {state}.

STEP 1 — Find their LinkedIn profile:
- Search Google: "{person_name}" "{entity_name}" LinkedIn
- If no results, try: "{person_name}" {state} LinkedIn
- Verify the profile matches (same company, same state, same role)
- From the profile, note: profile URL, headline, location, current position

STEP 2 — Gather professional context:
- From LinkedIn and any other professional sources you encounter:
  * Their career history and professional background
  * How long they've been with "{entity_name}"
  * Education and credentials
  * Industry experience and expertise
  * Any professional associations or board memberships

STEP 3 — Note any contact information that appears naturally:
- If an email, phone number, or website appears on LinkedIn, a company page,
  a business directory, or in search results — record it
- Do NOT perform separate searches specifically for contact details

Focus on building a professional profile useful for sales outreach context.
Only report verified information — do NOT fabricate any details."""

    try:
        client = AsyncBrowserUse(api_key=key)
        task_handle = await asyncio.wait_for(
            client.tasks.create_task(
                task=task,
                llm=BROWSER_USE_MODEL,
                schema=PersonExtraction,
                max_steps=PERSON_MAX_STEPS,
            ),
            timeout=30,
        )

        result = await asyncio.wait_for(
            task_handle.complete(),
            timeout=PERSON_TIMEOUT,
        )

        data = result.parsed_output
        elapsed = round(time.time() - start, 1)

        if data is None:
            logger.warning(f"Person search for '{person_name}' returned no structured output")
            return PersonResult(
                entity_name=entity_name,
                person_name=person_name,
                title=title,
                sos_address=address if address != "UNKNOWN" else "",
                error="No structured output returned",
            )

        logger.info(
            f"Person search for '{person_name}' @ '{entity_name}' completed in {elapsed}s "
            f"linkedin={'found' if data.linkedin_url else 'none'} "
            f"location='{data.linkedin_location or 'none'}'"
        )

        return PersonResult(
            entity_name=entity_name,
            person_name=person_name,
            title=data.current_title or title,
            sos_address=address if address != "UNKNOWN" else "",
            linkedin_url=data.linkedin_url or "",
            linkedin_location=data.linkedin_location or "",
            linkedin_headline=data.linkedin_headline or "",
            personal_phone=data.phone or "",
            business_phone="",
            email=data.email or "",
            home_address="",
            background=data.background or "",
            years_with_org=data.years_with_org or "",
        )

    except asyncio.TimeoutError:
        logger.warning(f"Person search timeout for '{person_name}' @ '{entity_name}'")
        return PersonResult(
            entity_name=entity_name,
            person_name=person_name,
            title=title,
            sos_address=address if address != "UNKNOWN" else "",
            error=f"Timeout after {PERSON_TIMEOUT}s",
        )
    except Exception as e:
        logger.error(f"Person search failed for '{person_name}' @ '{entity_name}': {e}")
        return PersonResult(
            entity_name=entity_name,
            person_name=person_name,
            title=title,
            sos_address=address if address != "UNKNOWN" else "",
            error=str(e)[:500],
        )
