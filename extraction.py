"""
Business list extraction from XLSX files (Google Places API export format).

The input is a structured spreadsheet of businesses pulled from Google Places.
This module reads it, pulls the entity_name + state per row, and preserves the
original row index so the output writer can append owner columns back to the
exact same rows.
"""

import io
import logging
import re

import openpyxl

logger = logging.getLogger("fdd-agent")


# Header aliases — case-insensitive. First match wins.
_NAME_HEADERS = ("business name", "name", "entity", "entity name", "company", "company name")
_LOCATION_HEADERS = ("location", "address", "street address")
_SEARCH_LOCATION_HEADERS = ("search location", "city", "city, state")
_PHONE_HEADERS = ("phone number", "phone")

# "Buda, TX 78610, USA" → TX
_STATE_FROM_ADDR = re.compile(r",\s*([A-Z]{2})\s+\d{5}")
# "Austin, TX" → TX
_STATE_FROM_CITYSTATE = re.compile(r",\s*([A-Z]{2})\s*$")


def _find_col(header_row: list[str], aliases: tuple[str, ...]) -> int:
    lowered = [(c or "").strip().lower() for c in header_row]
    for alias in aliases:
        if alias in lowered:
            return lowered.index(alias)
    return -1


def _extract_state(location: str, search_location: str) -> str:
    for source in (location, search_location):
        if not source:
            continue
        m = _STATE_FROM_ADDR.search(source)
        if m:
            return m.group(1)
        m = _STATE_FROM_CITYSTATE.search(source)
        if m:
            return m.group(1)
    return ""


def extract_businesses_from_xlsx(xlsx_bytes: bytes) -> list[dict]:
    """
    Read a Google Places business-list XLSX and return one dict per row:
        {row_index, entity_name, state, address, phone}

    `row_index` is the 1-based row number in the original sheet (header is row 1,
    so first data row is row_index=2). The output writer uses this to append
    owner-name columns back to the same rows.
    """
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        return []

    header = [str(c) if c is not None else "" for c in rows[0]]
    name_col = _find_col(header, _NAME_HEADERS)
    loc_col = _find_col(header, _LOCATION_HEADERS)
    search_loc_col = _find_col(header, _SEARCH_LOCATION_HEADERS)
    phone_col = _find_col(header, _PHONE_HEADERS)

    if name_col < 0:
        raise ValueError(
            f"Could not find a business-name column in the header. "
            f"Expected one of: {_NAME_HEADERS}. Got: {header}"
        )

    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        cells = [str(c).strip() if c is not None else "" for c in row]
        name = cells[name_col] if name_col < len(cells) else ""
        if not name or len(name) < 2:
            continue
        location = cells[loc_col] if 0 <= loc_col < len(cells) else ""
        search_location = cells[search_loc_col] if 0 <= search_loc_col < len(cells) else ""
        phone = cells[phone_col] if 0 <= phone_col < len(cells) else ""

        state = _extract_state(location, search_location)
        out.append({
            "row_index": i,
            "entity_name": name,
            "state": state,
            "address": location,
            "phone": phone,
        })

    logger.info(
        f"Loaded {len(out)} businesses from XLSX "
        f"(states found: {sorted({r['state'] for r in out if r['state']})})"
    )
    return out
