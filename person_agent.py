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
    user_context: str = "",
) -> PersonResult:
    """
    Research a person's professional background, focused on LinkedIn resolution.

    The primary goal is to find their LinkedIn profile, which provides:
    - Location (city-level) for downstream enrichment matching
    - Professional background for sales outreach context
    - Any contact info that appears naturally

    Contact info is NOT explicitly searched for — it's captured only
    if it shows up organically in search results or profile pages.

    `user_context` is free-text context provided at upload time (e.g.
    "This is a Del Taco FDD list — every entity is a Del Taco franchisee").
    It's prepended to the task prompt so the agent can disambiguate the
    correct person and verify them against the list's known pattern.
    """
    start = time.time()
    key = api_key or BROWSER_USE_API_KEY

    person_name = person.name
    title = person.title
    address = person.address

    has_context = bool((user_context or "").strip())
    user_context_block = ""
    if has_context:
        user_context_block = f"""═══ USER-PROVIDED LIST CONTEXT ═══
{user_context.strip()}

TREAT THIS CONTEXT AS AUTHORITATIVE for every person in this batch.
Two concrete ways you MUST use it:

1) SEARCH QUERY CONSTRUCTION — when building Google searches for this
   person, include the BRAND / FRANCHISE / PARENT-COMPANY name from
   the context above alongside the person's name. The LLC/shell name
   ("{entity_name}") is often invisible online; the brand name is how
   people are actually indexed on LinkedIn, news, and directories.

   Example — if the context says "This is a Del Taco FDD list", your
   queries should look like:
     '"{person_name}" "Del Taco" LinkedIn'
     '"{person_name}" Del Taco franchise {state}'
     '"{person_name}" Del Taco owner'
   BEFORE you fall back to the shell-LLC query.

2) PROFILE VERIFICATION — reject LinkedIn profiles that don't fit
   the context's pattern (e.g., reject a software-engineer profile
   when the context says this is a restaurant-franchise list), even
   if the name and state match.
═══ END LIST CONTEXT ═══

"""

    # Build state-aware context-brand queries only when user_context is present.
    if has_context:
        step1_queries = f"""- Search Google with queries built from the LIST CONTEXT above.
  Start with brand-first queries, since the shell LLC name is usually
  invisible online:
    1. '"{person_name}" <brand-from-context> LinkedIn'
    2. '"{person_name}" <brand-from-context> {state}'
    3. '"{person_name}" <brand-from-context> franchise owner'
  (Replace <brand-from-context> with the franchise/parent-company name
  extracted from the list context block above.)
- Only after those fail, fall back to the shell LLC:
    4. '"{person_name}" "{entity_name}" LinkedIn'
    5. '"{person_name}" {state} LinkedIn'"""
    else:
        step1_queries = f"""- Search Google: "{person_name}" "{entity_name}" LinkedIn
- If no results, try: "{person_name}" {state} LinkedIn"""

    task = f"""{user_context_block}Research the professional background of "{person_name}" who is
the {title} of "{entity_name}" in {state}.

STEP 1 — Find their LinkedIn profile:
{step1_queries}
- Verify the profile matches (same company or franchise, same state, same role)
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
