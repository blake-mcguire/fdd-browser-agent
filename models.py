"""
Pydantic models for SOS lookup results.
"""

from typing import List
from pydantic import BaseModel


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


class PersonEntry(BaseModel):
    """A person found via SOS (officer, member, or registered agent)."""
    name: str
    title: str = ""
    address: str = ""
    first_name: str = ""
    last_name: str = ""


class EntityRecord(BaseModel):
    """One row's pipeline state — input metadata + SOS findings."""
    entity_name: str
    state: str
    # 1-based row number in the original input XLSX. The output writer uses
    # this to append owner-name columns to the exact same row.
    original_row_index: int = 0
    address: str = ""
    source_file: str = ""

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

    people: List[PersonEntry] = []
