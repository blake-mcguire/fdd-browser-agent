# FDD LEAD ENRICHMENT AGENT — COMPLETE CONTEXT

## WHAT THIS PROJECT IS

A dockerized Python application that takes franchise disclosure documents (FDD PDFs) or generic lead lists (XLSX/CSV) and produces fully enriched lead sheets with company intelligence and personal contact information for every officer/agent associated with each franchise entity.

The end user drops a file in, the system figures out what it is, extracts the entities, deduplicates them, scrapes Secretary of State filings for officer/agent data, enriches company-level intelligence from the web, and researches each individual person — all running concurrently with managed parallelism.

**Delivered as:** A Docker container the client runs locally with their own API keys.

**Browser automation:** Browser Use (https://github.com/browser-use/browser-use) running locally.

**LLM:** Google Gemini via API (the client provides their own key).

---

## THE THREE INPUT TYPES (with real examples examined)

### Input Type 1: FDD PDF (Del Taco style)
- **File:** Del-Taco-2024.pdf (325 pages)
- **Franchisor:** Del Taco LLC
- **Entity table location:** Exhibit H, starts at page 257
- **Table columns:** Entity | Unit Number | Address | City | State | Office Number
- **Key characteristic:** Entity name is the LLC/company, listed ONCE PER FRANCHISE LOCATION. "Desert Taco, LLC" appears 20+ times, once per restaurant.
- **State grouping:** Entities grouped by state headers (Alabama, Arizona, etc.) with state code in the State column.
- **Sample rows:**
  ```
  Blue Bonnett Taco, Inc.     | 1397 | 3065 Pepperell Parkway      | Opelika     | AL | (706) 507-4200
  Desert Taco, LLC            | 1103 | 4115 W. Anthem Way          | Anthem      | AZ | (602) 708-3040
  Desert Taco, LLC            | 723  | 1483 North Dysart Road      | Avondale    | AZ | (602) 708-3040
  Ekstrom, Dennis (DTAZ, LLC) | 1244 | 1901 Pebble Creek Parkway   | Goodyear    | AZ | (602) 432-7070
  ```
- **Entity names can be:** LLCs ("Desert Taco, LLC"), Corporations ("Blue Bonnett Taco, Inc."), Individuals with LLCs in parens ("Ekstrom, Dennis (DTAZ, LLC)"), or bare individual names ("Brent Veach", "Gullat Michael Clay")
- **~458 total rows, needs dedup to ~30-50 unique entities**

### Input Type 2: FDD PDF (Jersey Mike's style)
- **File:** document_-_2026-03-25T164556_510.pdf (399 pages)
- **Franchisor:** A Sub Above, LLC (Jersey Mike's)
- **Entity table location:** Item 20 area, pages 213-290 (77 pages of entity data)
- **Table columns:** * | Primary Contact | Company | Street Address | Suite/Unit | City | State | Zip | Phone
- **Key characteristic:** Has BOTH a person name (Primary Contact) AND the company name per row. The `*` column marks something (possibly area developer).
- **Sample rows:**
  ```
  * | Fred Rosenberg    | JM Alaska, LLC                          | 2001 East 88th Ave | Suite 104 | Anchorage     | AK | 99507 | (907) 563-7777
  * | Kimberly A. Crowell| K & A Subs Alabaster, LLC              | 1114 1st St N      | Suite 300 | Alabaster     | AL | 35007 | (205) 729-2426
    | Chris Daniels     | ACD Subs Albertville LLC                | 128 AL-75          | Unit 3    | Albertville   | AL | 35951 | (256) 400-1241
  * | John McDonald     | LABE Restaurant Group, LLC              | 2081 South College St| Suite A  | Auburn        | AL | 36832 | (334) 591-6600
  ```
- **Entity names are the Company column. Primary Contact is a bonus — it's a person name the SOS step might also find.**
- **~2955 total location rows (per Item 20 summary), needs dedup to unique companies**

### Input Type 3: Generic Lead List (XLSX)
- **File:** locity_resturants.xlsx (1,722 rows)
- **No header row** — first row is data, not column names
- **Inferred columns (0-indexed):**
  - Col 0: Entity/Company Name
  - Col 1: Internal ID (CS###### or plain number)
  - Col 2-3: Rarely populated (IDs?)
  - Col 4: State (2-letter code)
  - Col 5: Type — "Set" or "Company"
  - Col 6: Employee count (integer)
  - Col 7: Revenue (float, dollars)
  - Col 8: Date or "-" dash
  - Col 9: Location count (integer)
  - Col 10: Another number or 0
  - Col 11: Status flag ("MLR" or null)
  - Col 12: "yes" or null
  - Col 13: "yes"/"almost" or null
  - Col 14: "yes" or null
  - Col 15: Free-text notes from human research
- **Already one row per entity (mostly)**
- **Has human-entered notes:** "chris' number is confirmed he is the Chief people officer", "Lost this client in 2024 to paylocity", "accounting firm", "controller confirmed on mobile"
- **These notes should be preserved and carried forward into the output**
- **Sample rows:**
  ```
  Coopers Hawk Intermediate Holding LLC | 15200    | IL | Company | 8915  | 585182.93 | 2015-10-19 | 2  | MLR/yes/almost/yes | chris' number is confirmed...
  HOSPITALITY SYRACUSE INC              | 307541   | MI | Company | 2462  | 551866.07 | 2025-03-31 | 5  |                    | Hospitality Restaurant Group, Inc
  Think Food Group LLC                  | CS314665 | DC | Set     | 2221  | 419695.58 | -          | 14 | yes/yes            | Lost this client in 2024...
  ```

### How to detect input type
- **PDF with "FRANCHISE DISCLOSURE DOCUMENT" on page 1** → FDD type
- **XLSX/CSV** → Generic lead list type
- FDD PDFs vary wildly in structure per franchisor. The entity table columns, location in the document, and naming conventions differ. The LLM must handle this dynamically.

---

## REQUIRED OUTPUT

### Output 1: Company Lead Sheet (one row per entity)

| Field | Source | Description |
|-------|--------|-------------|
| entity_name | FDD/XLSX | The LLC/Corp/entity name as listed |
| location_count | FDD dedup count | Number of franchise locations (times entity appears in FDD) |
| states_of_operation | FDD dedup | Comma-separated: "AZ,CA,NV" |
| state_of_formation | SOS | Where the entity is registered |
| sos_status | SOS | Active, Dissolved, Suspended, etc. |
| sos_filing_date | SOS | When filed |
| registered_agent | SOS | Name (if real person, not statutory service) |
| officers | SOS | JSON or multi-column: name, title, address per officer |
| company_website | Web search | If found |
| recent_news | Web search | Key developments: expansions, closures, leadership changes, acquisitions |
| company_notes | Web search | Anything else of note: PR, articles, awards |
| original_notes | XLSX only | Preserved human-entered notes from input file |
| source_file | Input | Which file this came from |
| franchisor | FDD | "Del Taco", "Jersey Mike's", etc. |

### Output 2: Person Lead Sheet (one row per officer/agent per entity)

| Field | Source | Description |
|-------|--------|-------------|
| entity_name | Parent company | Which entity this person is associated with |
| person_name | SOS | Full name from SOS filing |
| title | SOS | Officer title: CEO, Manager, Member, Registered Agent, etc. |
| sos_address | SOS | Address listed on SOS filing |
| linkedin_url | Web search | LinkedIn profile URL |
| personal_phone | Web search / people search | Personal/mobile phone number |
| business_phone | SOS/FDD/Web | Business line |
| email | Web search | Any email found |
| home_address | Web search / people search | Residential address |
| person_background | Web search | Info useful for outreach: "gynecology doctor with 48 years experience", "former CEO of XYZ Corp", professional background |
| years_with_org | Web search | If determinable |
| source_file | Input | Which file this came from |

### Output format
Both sheets delivered as XLSX (two tabs) and/or CSV files. Full audit trail as JSONL.

---

## ARCHITECTURE

### File Classification & Extraction Layer

```
Input File
    │
    ├── PDF detected
    │   ├── Check page 1 for "FRANCHISE DISCLOSURE DOCUMENT"
    │   │   ├── YES → FDD Pipeline
    │   │   │   ├── Extract franchisor name from page 1
    │   │   │   ├── Find entity table (Exhibit H, Item 20 area, etc.)
    │   │   │   │   This varies per franchisor — use LLM to identify
    │   │   │   ├── Chunk entity table pages
    │   │   │   ├── Send chunks to LLM for structured extraction
    │   │   │   │   Output: [{entity_name, address, city, state, phone}, ...]
    │   │   │   ├── Deduplicate by entity name
    │   │   │   │   Count locations per entity
    │   │   │   │   Build comma-separated state list per entity
    │   │   │   └── Output: Normalized entity list
    │   │   └── NO → Treat as unknown PDF, attempt generic extraction
    │   │
    └── XLSX/CSV detected
        ├── Read file, detect column mapping
        ├── Extract entity name, state, location count, existing notes
        ├── Normalize to same schema as FDD output
        └── Output: Normalized entity list
```

### Concurrent Processing Architecture

The system uses managed parallelism with queues. This is the critical design:

```
NORMALIZED ENTITY LIST (from extraction above)
    │
    ▼
┌─────────────────────────────────────────────┐
│         SOS SCRAPING POOL (5 concurrent)     │
│                                              │
│  Entity 1 + State → SOS Browser Instance 1  │
│  Entity 2 + State → SOS Browser Instance 2  │
│  Entity 3 + State → SOS Browser Instance 3  │
│  Entity 4 + State → SOS Browser Instance 4  │
│  Entity 5 + State → SOS Browser Instance 5  │
│                                              │
│  As each completes, next entity from queue   │
│  starts on the freed instance.               │
│                                              │
│  OUTPUT per entity:                          │
│  - Officers: [{name, title, address}, ...]   │
│  - Registered Agent: {name, address}         │
│  - Entity status, filing date                │
│  - FILTER: skip statutory agent services     │
│  - FILTER: determine if agent name is a      │
│    person or a company before proceeding     │
└──────────────┬──────────────────────────────┘
               │
               │ Each completed SOS result feeds TWO queues:
               │
        ┌──────┴──────┐
        ▼             ▼
┌──────────────┐ ┌───────────────────────────┐
│ COMPANY      │ │ PERSON SEARCH POOL        │
│ ENRICHMENT   │ │ (dynamic — one instance   │
│ POOL         │ │  per person per company)  │
│ (5 concurrent)│ │                           │
│              │ │ Company 1 had 3 officers: │
│ Company 1 →  │ │  → Person Instance A      │
│  search for: │ │  → Person Instance B      │
│  - articles  │ │  → Person Instance C      │
│  - news      │ │                           │
│  - PR posts  │ │ Company 2 had 2 officers: │
│  - website   │ │  → Person Instance D      │
│  - expansions│ │  → Person Instance E      │
│  - closures  │ │                           │
│  - leadership│ │ Each person search looks  │
│    changes   │ │ for:                      │
│              │ │  - LinkedIn URL           │
│ Company 2 →  │ │  - Phone number           │
│  (same)      │ │  - Email address          │
│              │ │  - Home address           │
│ ...          │ │  - Background info for    │
│              │ │    outreach context        │
└──────┬───────┘ └───────────┬───────────────┘
       │                     │
       ▼                     ▼
┌─────────────────────────────────────────────┐
│          FINAL ASSEMBLY                      │
│                                              │
│  Merge: Company Lead Sheet + Person Sheet    │
│  Write: XLSX with two tabs + JSONL audit     │
└─────────────────────────────────────────────┘
```

### Key concurrency rules:
1. **SOS pool: 5 concurrent browser instances.** Queue feeds entities one at a time as instances free up.
2. **Company enrichment pool: 5 concurrent browser instances.** Starts as soon as an entity completes SOS. Does NOT wait for all SOS to finish.
3. **Person search pool: dynamic.** Each company that completes SOS spawns N person search instances (one per human officer/agent found). These run concurrently with the company enrichment searches.
4. **Total browser instances at peak:** 5 (SOS) + 5 (company) + N (person) where N depends on how many officers the currently-processing companies have. May need a global cap (e.g., 15 total browser instances).

---

## TECHNOLOGY REQUIREMENTS

### Browser Use
- **Library:** `browser-use` (https://github.com/browser-use/browser-use)
- **Purpose:** All browser automation — SOS scraping, Google searches, site visits, people search
- **Runs locally** — no cloud browser service needed
- **Must handle:** CAPTCHAs (gracefully fail and retry/skip), bot detection, dynamic JS pages

### LLM
- **Provider:** Google Gemini API
- **Model:** gemini-2.0-flash or gemini-2.5-pro (configurable)
- **Used for:**
  1. FDD entity table extraction (chunked PDF text → structured JSON)
  2. FDD type detection and table location finding
  3. SOS page data extraction (browser page content → structured officers/agents)
  4. Company enrichment reasoning (search results → key findings)
  5. Person search reasoning (search results → contact data)
  6. Entity name classification (is this a person name or company name?)

### Docker
- **Base image:** Python 3.12 + Playwright/Chromium
- **Mounts:** Input directory, output directory
- **Environment variables:** `GEMINI_API_KEY`, optional config overrides
- **Single command:** `docker run -v ./input:/data/input -v ./output:/data/output -e GEMINI_API_KEY=xxx fdd-enrichment`

---

## SOS SCRAPING DETAILS

### What to extract from each state's Secretary of State
- Entity name (as registered)
- Entity type (LLC, Corp, LP, etc.)
- Status (Active, Inactive, Dissolved, etc.)
- Filing date
- Filing number
- Jurisdiction/state of formation
- **Registered Agent:** name + address
- **Officers/Members/Managers:** name + title + address for EACH one listed
- Principal office address
- Mailing address

### Critical filters BEFORE proceeding to person search
1. **Statutory agent services** — these are NOT people, do not research them:
   - CT Corporation System, Corporation Service Company (CSC), National Registered Agents (NRAI),
     Northwest Registered Agent, Cogency Global, United Agent Group, Incorp Services,
     The Corporation Trust Company, Registered Agents Inc, LegalInc, VCorp Services,
     Paracorp, Capitol Corporate Services, Harvard Business Services
   - Detection: match against known list + heuristic (name contains "Inc", "LLC", "Corp", "Services", "Group" and doesn't look like a person name)

2. **Company-name agents** — if the registered agent is itself a company (not a statutory service, but still not a person), skip it for person search but note it. Example: "Rodriguez Consulting LLC" as registered agent — that's not a person to research, but it might be related to the principals.

3. **Duplicate officers** — same person listed as both registered agent and officer, or same person with slightly different name formatting. Deduplicate before spawning person search instances.

### SOS sites by state (the browser agent navigates these)
The agent needs to handle different state portals. Some examples:
- **California:** bizfileonline.sos.ca.gov/search/business
- **Delaware:** icis.corp.delaware.gov/ecorp/entitysearch
- **Texas:** mycpa.cpa.state.tx.us/coa/
- **Florida:** search.sunbiz.org/Inquiry/CorporationSearch/ByName
- **New York:** appext20.dos.ny.gov/corp_public/CORPSEARCH.ENTITY_SEARCH_ENTRY
- **All other states:** Google search fallback: `"{entity name}" secretary of state {state name} filing`

The browser agent must handle each state's unique UI — form fields, search buttons, result links, detail pages. This is why it needs LLM reasoning, not hardcoded selectors.

---

## COMPANY ENRICHMENT SEARCH DETAILS

For each entity that passes SOS scraping, search the web for company-level intelligence:

### What to search for:
1. **Recent news and articles** — Google search: `"{company name}" news`
   - Expansions (new locations, new markets)
   - Closures (shutting down locations)
   - Leadership changes (new CEO, CFO departures)
   - Acquisitions or mergers
   - Lawsuits or regulatory issues
   - Awards or recognition

2. **Company website** — find the primary website URL

3. **PR and press releases** — search for press releases about the company

4. **General intelligence** — anything useful for sales outreach context

### Output format per company:
```json
{
  "entity_name": "Desert Taco, LLC",
  "website": "https://deserttacoaz.com",
  "recent_news_summary": "Expanded to 3 new locations in Phoenix metro in 2024. Named 'Best Franchise Operator' by Del Taco corporate.",
  "key_developments": [
    "Opened 3 new locations in 2024",
    "Awarded franchise operator of the year"
  ],
  "risk_signals": [],
  "notes": ""
}
```

---

## PERSON SEARCH DETAILS

For each HUMAN officer/agent found in SOS (after filtering out statutory agents and companies), search for personal contact information.

### Input per person:
- Full name (from SOS)
- Title/role (from SOS)
- Address (from SOS — may be business or residential)
- Associated company name
- State

### What to search for:
1. **LinkedIn URL** — Google: `"{person name}" "{company name}" LinkedIn`
2. **Phone number** — people search sites, business directories, Google
3. **Email address** — LinkedIn, company website, business directories
4. **Home address** — people search sites (using SOS address as a starting point if it looks residential)
5. **Background for outreach** — professional background, years of experience, specialties, education, other business affiliations

### Output format per person:
```json
{
  "entity_name": "Desert Taco, LLC",
  "person_name": "James Rodriguez",
  "title": "Manager",
  "sos_address": "1234 Elk Valley Rd, Crescent City, CA 95531",
  "linkedin_url": "https://linkedin.com/in/james-rodriguez-123",
  "personal_phone": "(555) 123-4567",
  "business_phone": "(602) 708-3040",
  "email": "jrodriguez@deserttaco.com",
  "home_address": "1234 Elk Valley Rd, Crescent City, CA 95531",
  "background": "Restaurant operator with 15 years in QSR. Previously managed 3 Subway locations in Northern California before acquiring Del Taco franchise rights in 2015.",
  "years_with_org": "9"
}
```

---

## FDD ENTITY EXTRACTION — LLM APPROACH

FDD entity tables vary per franchisor. The LLM must handle this dynamically.

### Strategy:
1. **Find the entity table** in the PDF:
   - Search for "Exhibit H", "List of Franchisees", "Franchisee Information", "Item 20" sections
   - Rasterize candidate pages and send to LLM to confirm: "Does this page contain a table of franchise entity names with addresses?"
   - Once found, identify the page range of the table

2. **Extract pages as text** using `pdftotext -layout` for the entity table pages

3. **Chunk the text** into ~4000 token chunks (roughly 5-10 pages per chunk depending on density)

4. **Send each chunk to LLM** with this instruction:
   ```
   Extract every franchise entity row from this text. Each row should have:
   - entity_name: The LLC, Inc, Corp, or individual name
   - address: Street address of the franchise location
   - city: City
   - state: 2-letter state code
   - phone: Phone number if present
   - unit_number: Store/unit number if present

   Some entities appear multiple times (once per location). Include every row.
   Return as a JSON array. If a row is ambiguous or partially cut off, include what you can.
   ```

5. **Merge all chunks** and deduplicate:
   - Group by normalized entity name (case-insensitive, strip whitespace)
   - Count occurrences = location_count
   - Collect unique states = states_of_operation (comma-separated)
   - Keep one representative phone number per entity

### Handling the two FDD formats observed:

**Del Taco format:** Entity name in first column, state code in State column. Entities repeat per location. State headers ("Alabama", "Arizona") appear as section dividers in the table.

**Jersey Mike's format:** Company name in "Company" column, PLUS a "Primary Contact" person name column. The primary contact is a bonus data point — save it as a potential officer name that might match SOS findings later. Entity names repeat per location.

---

## CONFIGURATION

```yaml
# config.yaml (or environment variables)
gemini:
  api_key: ${GEMINI_API_KEY}
  model: gemini-2.0-flash  # or gemini-2.5-pro for harder tasks

concurrency:
  sos_pool_size: 5          # concurrent SOS browser instances
  company_pool_size: 5      # concurrent company enrichment instances
  person_pool_max: 10       # max concurrent person search instances
  global_browser_cap: 20    # absolute max browser instances at once

browser:
  headless: true            # false for debugging
  timeout_seconds: 30       # per-navigation timeout

completion:
  max_steps_per_person: 8   # max reasoning loop steps for person search
  max_steps_per_company: 5  # max steps for company enrichment
  confidence_threshold: 70  # minimum confidence to mark as complete

output:
  directory: /data/output
  format: xlsx              # xlsx, csv, or both
  audit_log: true           # write JSONL audit trail

input:
  directory: /data/input
  watch: false              # if true, watch directory for new files
```

---

## DOCKER SETUP

```dockerfile
FROM python:3.12-slim

# Install Playwright dependencies
RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

# Create input/output mount points
RUN mkdir -p /data/input /data/output

ENTRYPOINT ["python", "run.py"]
CMD ["--input-dir", "/data/input", "--output-dir", "/data/output"]
```

**Usage:**
```bash
# Build
docker build -t fdd-enrichment .

# Run with a single file
docker run --rm \
  -v ./my_files:/data/input \
  -v ./results:/data/output \
  -e GEMINI_API_KEY=your-key-here \
  fdd-enrichment

# Run with specific file
docker run --rm \
  -v ./my_files:/data/input \
  -v ./results:/data/output \
  -e GEMINI_API_KEY=your-key-here \
  fdd-enrichment --file /data/input/Del-Taco-2024.pdf

# Run headful for debugging
docker run --rm \
  -v ./my_files:/data/input \
  -v ./results:/data/output \
  -e GEMINI_API_KEY=your-key-here \
  -e DISPLAY=:0 \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  fdd-enrichment --headful
```

---

## IMPLEMENTATION ORDER

### Phase 1: File Classification & FDD Extraction
1. Input file detection (PDF vs XLSX/CSV)
2. FDD detection (check page 1 for franchise disclosure language)
3. Entity table finder (search for Exhibit H / Item 20 / franchisee list)
4. LLM-based entity extraction from PDF chunks
5. Deduplication and normalization
6. XLSX/CSV reader with column auto-detection
7. Unified output: normalized entity list with location_count and states

### Phase 2: SOS Scraping Pool
1. Browser Use integration with Gemini as reasoning LLM
2. Concurrent pool manager (asyncio semaphore, 5 slots)
3. State-aware SOS navigation (the agent figures out each state's portal)
4. Officer/agent extraction from SOS detail pages
5. Statutory agent filtering
6. Person vs company name classification
7. Result queuing for downstream pools

### Phase 3: Company Enrichment Pool
1. Google search agent for company news/articles
2. Website finder
3. News summarization via LLM
4. Concurrent pool (5 slots), fed from SOS completion queue

### Phase 4: Person Search Pool
1. Dynamic instance spawning (one per human officer per company)
2. LinkedIn search
3. Phone/email search (people search sites, directories)
4. Background research
5. Concurrent pool with global cap

### Phase 5: Assembly & Output
1. Merge all results into company sheet + person sheet
2. XLSX writer (two tabs)
3. JSONL audit trail
4. Docker packaging

### Phase 6: Completion Criteria
1. Satisfaction scoring for person search (when to stop)
2. Hard/soft stopping rules
3. Productivity tracking (consecutive unproductive steps → stop)
4. Budget guards (token limits per entity)

---

## KEY DESIGN DECISIONS

1. **Gemini, not Claude** — the client wants Gemini as the LLM. Use google-generativeai Python SDK or the OpenAI-compatible endpoint.

2. **Browser Use, not raw Playwright** — Browser Use handles the LLM ↔ browser interaction loop. We provide the task description and context; Browser Use manages navigation, clicking, typing, and page reading.

3. **SOS before person search** — SOS is the authority for who the officers are. Don't guess from Google; get the actual filing data first.

4. **Parallel, not sequential** — 5 SOS instances, 5 company enrichment instances, N person instances all running concurrently. Use asyncio with semaphores for pool management.

5. **Entity table extraction via LLM, not hardcoded parsing** — every franchisor's FDD has a different table layout. The LLM reads the text/image and extracts structured data. This is the only approach that generalizes.

6. **Statutory agent filtering is critical** — without it, the system wastes browser instances researching "CT Corporation System" as if it were a person.

7. **Person vs company classification on agent names** — SOS registered agents can be people or companies. Must classify before spawning person search.

8. **Preserve existing human notes** — the XLSX input has notes from prior research. These must carry through to the output, not be overwritten.

9. **Completion criteria are code-enforced** — the LLM can suggest stopping, but hard rules (minimum phone number found, max steps, consecutive unproductive steps) are checked in code.

10. **Docker-first delivery** — the client runs this locally. No cloud dependencies except the Gemini API.
