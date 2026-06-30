# BUSINESS OWNER LOOKUP

## WHAT THIS PROJECT IS

A local Python service that takes a structured XLSX of businesses (the standard
Google Places API export shape) and appends owner names from each business's
Secretary of State filing.

End user drops an XLSX in, the system looks up each business on its state's
SOS portal, extracts the human officers / members / non-statutory registered
agent, and writes a copy of the input file with `Owner 1`, `Owner 2`, … columns
appended.

The pipeline is SOS-only. Earlier versions of this project did FDD entity
extraction, company-level web enrichment, and per-person research; those stages
have been removed. The system now does one thing: business name + state →
owner names.

## INPUT FORMAT

Expected XLSX header (row 1):

| Business Name | Location | Phone Number | Search Term | Search Location | Source URL | Date Collected |

`Business Name` is what the SOS agent searches for. `Location` is parsed for the
two-letter state code (`", XX  ddddd"`). If that fails, `Search Location`
(`"City, ST"`) is the fallback.

Header detection is alias-based (case-insensitive). Other reasonable header
spellings — `Name`, `Company`, `Address` — also work.

## OUTPUT

The original XLSX with new trailing columns: `Owner 1`, `Owner 2`, …, `Owner N`,
where N is the highest officer count across all SOS results. Rows with no
SOS hit get blank owner cells. A JSONL audit trail (full SOS payload per row)
is written alongside the XLSX for debugging.

## ARCHITECTURE

```
XLSX upload
    │
    ▼
[ extraction.py ]  read header, map columns, parse state
    │
    ▼
[ SOS pool ]       one batch per state, N concurrent browser instances
    │              (sos_agent.py, sos_portal_instructions.py)
    │
    │  Each row:
    │   • Browser-use agent visits the state's SOS portal
    │   • Searches for the business name
    │   • Extracts officers, members, registered agent
    │   • Validator: best-of-N across retry attempts; statutory-agent
    │     no-officer results accepted as-is
    │
    ▼
[ build_people_list ]   drop statutory agents (CT Corp, NRAI, COGENCY, etc.)
    │                   and entries whose last token is a biz suffix
    ▼
[ xlsx_builder.py ]     open original XLSX, append Owner 1..N columns,
                        write modified bytes
```

## KEY FILES

- `server.py` — FastAPI app, single SOS dispatcher pool, job state
- `extraction.py` — XLSX reader (no PDF, no LLM column inference)
- `sos_agent.py` — browser-use SOS lookup with retry + statutory-agent-aware validator
- `sos_portal_instructions.py` — per-state portal walkthroughs (50 states)
- `xlsx_builder.py` — output writer (preserves original sheet, appends Owner cols)
- `models.py` — Pydantic models (`SOSResult`, `EntityRecord`, `Officer`, `PersonEntry`)
- `config.py` — env vars, per-state validation criteria, statutory agent list

## DEPRECATED (PRESENT BUT UNUSED)

`company_agent.py`, `person_agent.py`, `CompanyResult`, `PersonResult` —
left in place from the FDD-era enrichment flow. Not imported by server.py.
Safe to delete in a future cleanup.

## RUNNING LOCALLY

```bash
.venv/bin/python server.py --port 8000
```

Then open http://localhost:8000, drop the XLSX, hit Run Lookup, download the
result when complete.
