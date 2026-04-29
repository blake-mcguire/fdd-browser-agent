"""
Entity extraction from FDD PDFs and XLSX lead lists.
Handles both input types → normalized entity list with dedup.
"""

import asyncio
import io
import json
import logging
import re
from collections import Counter, defaultdict

import pdfplumber
import openpyxl

from config import BIZ_SUFFIXES
from llm import call_extraction, call_structure, GeminiOverloadedError, GeminiRateLimitError, APIKeyDeadError

logger = logging.getLogger("fdd-agent")


# ── PDF Entity Extraction ─────────────────────────────────────

async def extract_entities_from_pdf(pdf_bytes: bytes, api_key: str) -> list[dict]:
    """
    Extract all franchisee entities from an FDD PDF.

    Strategy:
      1. Extract all page texts
      2. Find the entity table section (Exhibit H, Item 20, etc.)
      3. Chunk and send to Gemini for structured extraction
      4. Merge all chunks
    """
    pages: list[tuple[int, str]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total = len(pdf.pages)
        logger.info(f"PDF has {total} pages")
        for i, page in enumerate(pdf.pages):
            t = page.extract_text() or ""
            if t.strip():
                pages.append((i, t))

    if not pages:
        logger.error("No text extracted from PDF")
        return []

    # ── Find the franchisee list section ──
    # Strategy: find the first page with a high density of entity names.
    # The actual table has many LLCs/Incs per page, not just one mention.
    ENTITY_SIGNALS = ["llc", "inc.", "corp", "l.p.", "ltd"]
    TABLE_HEADER_SIGNALS = [
        "franchisee", "franchisees", "entity", "primary contact",
        "company", "unit", "exhibit h", "exhibit",
    ]
    STATE_CODES = [
        " al ", " ak ", " az ", " ar ", " ca ", " co ", " ct ", " de ",
        " fl ", " ga ", " hi ", " id ", " il ", " in ", " ia ", " ks ",
        " ky ", " la ", " me ", " md ", " ma ", " mi ", " mn ", " ms ",
        " mo ", " mt ", " ne ", " nv ", " nh ", " nj ", " nm ", " ny ",
        " nc ", " nd ", " oh ", " ok ", " or ", " pa ", " ri ", " sc ",
        " sd ", " tn ", " tx ", " ut ", " vt ", " va ", " wa ", " wv ",
        " wi ", " wy ", " dc ",
    ]

    def _entity_density(text: str) -> int:
        """Count entity signal occurrences on a page."""
        lower = text.lower()
        return sum(lower.count(s) for s in ENTITY_SIGNALS)

    def _has_state_codes(text: str) -> bool:
        lower = " " + text.lower() + " "
        return sum(1 for sc in STATE_CODES if sc in lower) >= 2

    # Pass 1: Find the first page (from back half forward) with high entity density
    # A real franchisee table page has 10+ entity names — contract pages rarely exceed 5
    section_start_idx = None
    mid = len(pages) // 2
    search_order = pages[mid:] + pages[:mid]

    for orig_idx, text in search_order:
        density = _entity_density(text)
        if density >= 10 and _has_state_codes(text):
            section_start_idx = orig_idx
            logger.info(f"Franchisee table found at page {orig_idx + 1} (entity density={density})")
            break

    # Pass 2: If no dense page found, look for "Exhibit H" or table headers
    # then scan forward to find where the actual data starts
    if section_start_idx is None:
        for orig_idx, text in search_order:
            lower = text.lower()
            if any(h in lower for h in TABLE_HEADER_SIGNALS):
                # Check this page and the next few for entity density
                for check_idx in range(orig_idx, min(orig_idx + 10, len(pages))):
                    check_text = next((t for i, t in pages if i == check_idx), "")
                    if _entity_density(check_text) >= 8:
                        section_start_idx = check_idx
                        logger.info(f"Franchisee section header at page {orig_idx + 1}, "
                                    f"data starts at page {check_idx + 1}")
                        break
                if section_start_idx is not None:
                    break

    if section_start_idx is None:
        logger.warning("No franchisee section found — scanning last 30% of document")
        start = int(len(pages) * 0.7)
        section_start_idx = pages[start][0] if start < len(pages) else pages[-1][0]

    # Only include pages from section start that have entity content
    # Stop when we hit 3+ consecutive pages with zero entity density
    section_texts = []
    zero_streak = 0
    for orig_idx, text in pages:
        if orig_idx < section_start_idx:
            continue
        density = _entity_density(text)
        if density == 0:
            zero_streak += 1
            if zero_streak >= 3 and section_texts:
                logger.info(f"Entity table ends at page {orig_idx - 1} (3 empty pages)")
                break
            # Include sparse pages in case they're state headers or page breaks
            section_texts.append(text)
        else:
            zero_streak = 0
            section_texts.append(text)

    # ── Chunk text ──
    CHUNK_CHARS = 40_000  # ~10-15 pages per chunk — GPT-4o mini handles this reliably
    full_section = "\n".join(section_texts)
    logger.info(f"Franchisee section: {len(full_section):,} chars across {len(section_texts)} pages")

    chunks = []
    for i in range(0, len(full_section), CHUNK_CHARS):
        chunk = full_section[i:i + CHUNK_CHARS]
        if chunk.strip():
            chunks.append(chunk)

    if not chunks:
        logger.error("No text in franchisee section")
        return []

    system_prompt = (
        "You are an expert at extracting structured data from Franchise Disclosure Documents (FDDs).\n\n"
        "You will receive text from the franchisee list section of an FDD PDF. "
        "This section lists all current franchisees with their locations.\n\n"
        "This table may be labeled as:\n"
        "- Exhibit H, H-1, or any other letter/number\n"
        "- Schedule A, B, etc.\n"
        "- List of Current Franchisees\n"
        "- Franchisee Information\n"
        "- Or embedded directly under Item 20\n\n"
        "Your job: Extract EVERY franchisee entity and their US state.\n\n"
        "Rules:\n"
        "- Entities can be: LLC names, corporations, partnerships, OR individual person names\n"
        "- Entity names may span multiple lines — reassemble them\n"
        "- States: always output as 2-letter codes (e.g. CA, TX, NY)\n"
        "- Each location is a separate entry even if the same entity appears multiple times\n"
        "- Ignore: addresses, phones, store/unit numbers, page headers/footers\n"
        "- Do NOT include former franchisees (only current)\n"
        "- Do NOT invent entities — only extract what exists\n\n"
        "Return ONLY a JSON array, no other text:\n"
        '[{"entity":"Name","state":"XX"},{"entity":"Name","state":"XX"}]\n\n'
        'If no franchisee table is present in this chunk, return: []'
    )

    all_entities: list[dict] = []
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            await asyncio.sleep(10)
        logger.info(f"Entity extraction chunk {idx + 1}/{len(chunks)} ({len(chunk):,} chars)")
        user_prompt = f"Extract all franchisee entity-state pairs from this FDD section:\n\n{chunk}"
        try:
            raw = await call_extraction(
                system=system_prompt,
                user=user_prompt,
                api_key=api_key,
                max_tokens=16384,
            )
            chunk_entities = _parse_entity_list(raw)
            logger.info(f"Chunk {idx + 1}: found {len(chunk_entities)} entities")
            all_entities.extend(chunk_entities)
        except (GeminiOverloadedError, GeminiRateLimitError, APIKeyDeadError) as e:
            # Gemini is down — abort immediately, don't waste time on remaining chunks
            logger.error(f"Entity extraction aborted at chunk {idx + 1}/{len(chunks)}: {e}")
            raise
        except Exception as e:
            logger.error(f"Entity extraction chunk {idx + 1} failed: {e}")

    logger.info(f"Total raw entities: {len(all_entities)}")
    return all_entities


# ── XLSX Entity Extraction ────────────────────────────────────

async def extract_entities_from_xlsx(xlsx_bytes: bytes, api_key: str) -> list[dict]:
    """
    Read XLSX and ask GPT-4o mini to map columns to entity/state/notes.
    Preserves human-entered notes from the input.
    """
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= 2000:
            break
        rows.append([str(c) if c is not None else "" for c in row])
    wb.close()

    if not rows:
        return []

    header = rows[0]
    sample = rows[1:21]

    raw = await call_structure(
        prompt=(
            f"Spreadsheet header row: {json.dumps(header)}\n"
            f"First 20 data rows: {json.dumps(sample)}\n\n"
            "Map the columns to franchisee entity data.\n"
            "Return a JSON array of objects with these keys:\n"
            '  entity_name, state (2-letter), address, notes (any human-entered notes/comments)\n'
            "Include ALL data rows, not just the sample.\n"
            "If there is no header row (first row looks like data), treat all rows as data.\n"
            "Preserve any free-text notes columns exactly as they appear.\n\n"
            f"All rows: {json.dumps(rows)}"
        ),
        system="You are a data analyst. Return a raw JSON array only.",
        api_key=api_key,
    )
    return _parse_entity_list(raw)


# ── JSON Parsing ──────────────────────────────────────────────

def _parse_entity_list(raw: str) -> list[dict]:
    """
    Parse LLM entity extraction response.
    Handles both key formats: entity/entity_name, truncated JSON, etc.
    """
    cleaned = re.sub(r"```json\s*", "", raw)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()

    if cleaned.startswith("{"):
        try:
            obj = json.loads(cleaned)
            if "error" in obj:
                logger.warning(f"LLM returned error: {obj.get('reason', obj['error'])}")
                return []
        except Exception:
            pass

    i = cleaned.find("[")
    j = cleaned.rfind("]")

    if i != -1 and j == -1:
        logger.warning("Entity list appears truncated — attempting recovery")
        last_brace = cleaned.rfind("}")
        if last_brace > i:
            cleaned = cleaned[i:last_brace + 1] + "]"
            j = len(cleaned) - 1
        else:
            return []

    if i != -1 and j > i:
        try:
            items = json.loads(cleaned[i:j + 1])
        except json.JSONDecodeError:
            last_brace = cleaned.rfind("}")
            if last_brace > i:
                try:
                    items = json.loads(cleaned[i:last_brace + 1] + "]")
                except Exception:
                    return []
            else:
                return []

        out = []
        for x in items:
            name = x.get("entity_name") or x.get("entity") or ""
            state = x.get("state") or ""
            address = x.get("address") or ""
            notes = x.get("notes") or ""
            if name and len(name.strip()) >= 2:
                out.append({
                    "entity_name": name.strip(),
                    "state": state.strip().upper()[:2],
                    "address": address.strip(),
                    "notes": notes.strip(),
                })
        return out

    # Fallback: try as dict with nested key
    try:
        d = json.loads(cleaned)
        if isinstance(d, dict):
            for k in ("entities", "franchisees", "results"):
                if k in d:
                    return _parse_entity_list(json.dumps(d[k]))
    except Exception:
        pass
    return []


# ── Deduplication ─────────────────────────────────────────────

def dedup_entities(entities: list[dict]) -> list[dict]:
    """
    Group by entity name (case-insensitive):
    - Count locations per entity
    - Build comma-separated state list
    - Preserve notes from first occurrence
    """
    groups: dict[str, dict] = {}
    for e in entities:
        name = e.get("entity_name", "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in groups:
            groups[key] = {
                "entity_name": name,
                "states": [],
                "address": e.get("address", ""),
                "notes": e.get("notes", ""),
                "count": 0,
            }
        st = e.get("state", "").strip().upper()[:2]
        if st:
            groups[key]["states"].append(st)
        groups[key]["count"] += 1
        if not groups[key]["address"] and e.get("address"):
            groups[key]["address"] = e["address"]
        if not groups[key]["notes"] and e.get("notes"):
            groups[key]["notes"] = e["notes"]

    out = []
    for g in groups.values():
        if g["states"]:
            primary_state = Counter(g["states"]).most_common(1)[0][0]
        else:
            primary_state = ""
        out.append({
            "entity_name": g["entity_name"],
            "state": primary_state,
            "address": g["address"],
            "notes": g["notes"],
            "num_locations": g["count"],
            "all_states": ",".join(sorted(set(g["states"]))),
        })

    out.sort(key=lambda x: x["num_locations"], reverse=True)
    return out


# ── Business-vs-person classification ────────────────────────
#
# Entities whose "name" is actually a human's name (no LLC/Inc/Corp/LP/etc.
# suffix) have a ~100% failure rate on SOS portals — they either error out
# entirely or return garbage officer/agent data attached to some unrelated
# entity that happened to share a surname.  Route those to a manual-review
# bucket before SOS kicks off.

_BIZ_SUFFIX_SET = {s.upper().rstrip(".") for s in BIZ_SUFFIXES}


def _token_is_biz_suffix(token: str) -> bool:
    """
    Return True if a token matches a known corporate suffix.

    All-lowercase tokens are deliberately rejected so everyday words like
    'group' in "Randy Taylor and group" don't count as the corporate
    'Group' suffix.  A real corporate suffix in an FDD entity name is
    almost always capitalized or all-caps.
    """
    if not token or token == token.lower():
        return False
    normalized = token.upper().rstrip(".")
    return normalized in _BIZ_SUFFIX_SET


def classify_entity_type(entity_name: str) -> str:
    """
    Classify an entity name as 'business' or 'person'.

    An entity is a BUSINESS if at least one capitalized token in the name
    (INCLUDING any content inside parentheses) matches a known corporate
    suffix (LLC, Inc, Corp, LLP, LP, Group, Holdings, Partners, etc.).

    Paren content is checked because FDDs often render rows like
    'Ekstrom, Dennis (DTAZ, LLC)' — the left side is a person but the
    parens contain the real LLC, so it is still a valid SOS target.

    Anything without such a suffix is classified 'person' and routed to
    manual review rather than pushed through the SOS pipeline.
    """
    if not entity_name or not entity_name.strip():
        return "person"
    tokens = re.findall(r"[A-Za-z.&']+", entity_name)
    for tok in tokens:
        if _token_is_biz_suffix(tok):
            return "business"
    return "person"


def split_entities_by_type(
    entities: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Split a deduped entity list into (business_entities, person_entities).

    Business entities continue into the SOS pipeline.
    Person entities are flagged for manual web search and skipped from SOS
    to avoid burning browser/LLM budget on lookups that are guaranteed to
    fail or return wrong-entity data.
    """
    businesses: list[dict] = []
    persons: list[dict] = []
    for e in entities:
        kind = classify_entity_type(e.get("entity_name", ""))
        if kind == "business":
            businesses.append(e)
        else:
            persons.append(e)
    return businesses, persons
