"""
Pydantic models for SOS, company enrichment, and person search results.
"""

from typing import Optional, List
from pydantic import BaseModel


# ── SOS Models ────────────────────────────────────────────────

class Officer(BaseModel):
    name: str
    title: str = "UNKNOWN"
    address: str = "UNKNOWN"


class SOSResult(BaseModel):
    entity_name: str
    state: str
    registered_agent: str = "UNKNOWN"
    agent_address: str = "UNKNOWN"
    entity_status: str = "UNKNOWN"
    formation_date: str = "UNKNOWN"
    entity_type: str = "UNKNOWN"
    dba_name: str = "UNKNOWN"
    officers: List[Officer] = []
    source_url: str = ""
    confidence: str = "LOW"
    raw_text: str = ""
    error: str = ""


# ── Company Enrichment Models ─────────────────────────────────

class CompanyResult(BaseModel):
    entity_name: str
    website: str = ""
    recent_news_summary: str = ""
    key_developments: List[str] = []
    risk_signals: List[str] = []
    notes: str = ""
    error: str = ""


# ── Person Search Models ──────────────────────────────────────

class PersonResult(BaseModel):
    entity_name: str
    person_name: str
    title: str = ""
    sos_address: str = ""
    linkedin_url: str = ""
    linkedin_location: str = ""      # city/metro from LinkedIn — key for downstream enrichment
    linkedin_headline: str = ""
    personal_phone: str = ""
    business_phone: str = ""
    email: str = ""
    home_address: str = ""
    background: str = ""
    years_with_org: str = ""
    error: str = ""


# ── Unified Entity Record (used during pipeline processing) ──

class PersonEntry(BaseModel):
    """A person found via SOS, before enrichment."""
    name: str
    title: str = ""
    address: str = ""
    first_name: str = ""
    last_name: str = ""


class EntityRecord(BaseModel):
    """Full pipeline state for one entity."""
    entity_name: str
    state: str
    num_locations: int = 1
    all_states: str = ""
    address: str = ""
    original_notes: str = ""
    source_file: str = ""
    franchisor: str = ""

    # SOS results
    registered_agent: str = "UNKNOWN"
    agent_address: str = "UNKNOWN"
    entity_status: str = "UNKNOWN"
    formation_date: str = "UNKNOWN"
    entity_type: str = "UNKNOWN"
    dba_name: str = "UNKNOWN"
    sos_source_url: str = ""
    sos_confidence: str = ""
    sos_error: str = ""

    # Company enrichment
    company: Optional[CompanyResult] = None

    # People found + enriched
    people: List[PersonEntry] = []
    person_results: List[PersonResult] = []
