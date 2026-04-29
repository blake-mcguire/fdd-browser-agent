"""
Shared configuration — env vars, constants, SOS registry, statutory agents.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Browser Use Cloud (company + person agents) ─────────────
BROWSER_USE_API_KEY = os.getenv("BROWSER_USE_API_KEY", "")
BROWSER_USE_MODEL = os.getenv("BROWSER_USE_MODEL", "browser-use-2.0")

# ── Google Gemini (SOS browser agent + PDF extraction) ───────
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ── SOS Browser Agent LLM (local browser-use with Gemini) ───
SOS_BROWSER_MODEL = os.getenv("SOS_BROWSER_MODEL", "gemini-2.5-flash")

# ── State Portal Credentials (some portals require login) ────
MI_SOS_USER = os.getenv("MI_SOS_USER", "")
MI_SOS_PASS = os.getenv("MI_SOS_PASS", "")

# ── Browser ──────────────────────────────────────────────────
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() in ("true", "1", "yes")

# ── Pipeline toggles ──────────────────────────────────────────
# Temporarily sever company & person enrichment while tuning SOS.
# Set to "true" to re-enable downstream enrichment.
ENRICHMENT_ENABLED = os.getenv("ENRICHMENT_ENABLED", "false").lower() in ("true", "1", "yes")

# ── Concurrency ───────────────────────────────────────────────
SOS_CONCURRENCY = int(os.getenv("SOS_CONCURRENCY", "3"))
COMPANY_CONCURRENCY = int(os.getenv("COMPANY_CONCURRENCY", "5"))
PERSON_CONCURRENCY_MAX = int(os.getenv("PERSON_CONCURRENCY_MAX", "10"))
GLOBAL_BROWSER_CAP = int(os.getenv("GLOBAL_BROWSER_CAP", "20"))

# ── Timeouts ──────────────────────────────────────────────────
SOS_TIMEOUT = int(os.getenv("SOS_TIMEOUT", "300"))
COMPANY_TIMEOUT = int(os.getenv("COMPANY_TIMEOUT", "180"))
PERSON_TIMEOUT = int(os.getenv("PERSON_TIMEOUT", "180"))

# ── Agent step limits ─────────────────────────────────────────
SOS_MAX_STEPS = int(os.getenv("SOS_MAX_STEPS", "15"))
SOS_INTER_ENTITY_DELAY = float(os.getenv("SOS_INTER_ENTITY_DELAY", "2.0"))  # seconds between entities per worker

# ── SOS result validation + retry ─────────────────────────────
# Number of extra attempts allowed when the first run returns an incomplete
# SOS result (missing required fields for the state). On retry, the previous
# failure reason is injected into the agent's task prompt.
SOS_VALIDATION_RETRIES = int(os.getenv("SOS_VALIDATION_RETRIES", "1"))

# Per-state success criteria. Each entry specifies which SOSResult fields MUST
# be populated (not empty, not "UNKNOWN") for a result to count as valid, and
# whether at least one officer is required.
#
# States absent from this dict use SOS_DEFAULT_CRITERIA.
# Tune these as you refine each state's walkthrough.
SOS_DEFAULT_CRITERIA = {
    "required_fields": ["entity_status", "registered_agent"],
    "require_officers": True,
}
SOS_SUCCESS_CRITERIA = {
    # Paywalled / minimal-data states (officers not obtainable or not present)
    "ME": {"required_fields": ["entity_status"],                         "require_officers": False},
    "SC": {"required_fields": ["entity_status", "registered_agent"],     "require_officers": False},
    "TN": {"required_fields": ["entity_status", "registered_agent"],     "require_officers": False},
    "TX": {"required_fields": ["entity_status", "registered_agent"],     "require_officers": False},
    "WI": {"required_fields": ["entity_status", "registered_agent"],     "require_officers": False},
    "WY": {"required_fields": ["entity_status", "registered_agent"],     "require_officers": True},
    # PA sidebar has status + officers but no registered agent
    "PA": {"required_fields": ["entity_status"],                         "require_officers": True},
    # AR often paywalls officer data
    "AR": {"required_fields": ["entity_status", "registered_agent"],     "require_officers": False},
    # MD "Resident Agent" only — officers behind annual report filings
    "MD": {"required_fields": ["entity_status", "registered_agent"],     "require_officers": False},
    # HI / MA — officers usually exposed, agent required
    # (use default)
}
COMPANY_MAX_STEPS = int(os.getenv("COMPANY_MAX_STEPS", "10"))
PERSON_MAX_STEPS = int(os.getenv("PERSON_MAX_STEPS", "12"))

# ── Output ────────────────────────────────────────────────────
MAX_CONTACTS = int(os.getenv("MAX_CONTACTS", "8"))

# ── SOS URL Registry ─────────────────────────────────────────
SOS_REGISTRY = {
    "AL": {"name": "Alabama", "url": "https://www.sos.alabama.gov/government-records/business-entity-records"},
    "AK": {"name": "Alaska", "url": "https://commerce.alaska.gov/cbp/main/search/entities"},
    "AZ": {"name": "Arizona", "url": "https://arizonabusinesscenter.azcc.gov/businesssearch"},
    "AR": {"name": "Arkansas", "url": "https://sos-corp-search.ark.org/corps"},
    "CA": {"name": "California", "url": "https://bizfileonline.sos.ca.gov/search/business"},
    "CO": {"name": "Colorado", "url": "https://www.sos.state.co.us/ucc/pages/biz/bizSearch.xhtml"},
    "CT": {"name": "Connecticut", "url": "https://service.ct.gov/business/s/onlinebusinesssearch"},
    "DE": {"name": "Delaware", "url": "https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx"},
    "FL": {"name": "Florida", "url": "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"},
    "GA": {"name": "Georgia", "url": "https://ecorp.sos.ga.gov/BusinessSearch"},
    "HI": {"name": "Hawaii", "url": "https://hbe.ehawaii.gov/documents/search.html"},
    "ID": {"name": "Idaho", "url": "https://sosbiz.idaho.gov/search/business"},
    "IL": {"name": "Illinois", "url": "https://apps.ilsos.gov/businessentitysearch/"},
    "IN": {"name": "Indiana", "url": "https://bsd.sos.in.gov/publicbusinesssearch"},
    "IA": {"name": "Iowa", "url": "https://sos.iowa.gov/search/business/search.aspx"},
    "KS": {"name": "Kansas", "url": "https://www.sos.ks.gov/eforms/BusinessEntity/Search.aspx"},
    "KY": {"name": "Kentucky", "url": "https://sosbes.sos.ky.gov/BusSearchNProfile/search.aspx"},
    "LA": {"name": "Louisiana", "url": "https://coraweb.sos.la.gov/commercialsearch/commercialsearch.aspx"},
    "ME": {"name": "Maine", "url": "https://apps3.web.maine.gov/nei-sos-icrs/ICRS?MainPage=x"},
    "MD": {"name": "Maryland", "url": "https://egov.maryland.gov/BusinessExpress/EntitySearch"},
    "MA": {"name": "Massachusetts", "url": "https://corp.sec.state.ma.us/corpweb/CorpSearch/CorpSearch.aspx"},
    "MI": {"name": "Michigan", "url": "https://mibusinessregistry.lara.state.mi.us/search/business"},
    "MN": {"name": "Minnesota", "url": "https://mblsportal.sos.mn.gov/Business/Search"},
    "MS": {"name": "Mississippi", "url": "https://corp.sos.ms.gov/corp/portal/c/page/corpBusinessIdSearch/portal.aspx"},
    "MO": {"name": "Missouri", "url": "https://bsd.sos.mo.gov/BusinessEntity/BESearch"},
    "MT": {"name": "Montana", "url": "https://biz.sos.mt.gov/search"},
    "NE": {"name": "Nebraska", "url": "https://www.nebraska.gov/sos/corp/corpsearch.cgi"},
    "NV": {"name": "Nevada", "url": "https://esos.nv.gov/EntitySearch/OnlineEntitySearch"},
    "NH": {"name": "New Hampshire", "url": "https://quickstart.sos.nh.gov/online/BusinessInquire"},
    "NJ": {"name": "New Jersey", "url": "https://njportal.com/dor/businessrecords"},
    "NM": {"name": "New Mexico", "url": "https://enterprise.sos.nm.gov/search"},
    "NY": {"name": "New York", "url": "https://apps.dos.ny.gov/publicInquiry/"},
    "NC": {"name": "North Carolina", "url": "https://sosnc.gov/online_services/search/by_title_702"},
    "ND": {"name": "North Dakota", "url": "https://firststop.sos.nd.gov/search/business"},
    "OH": {"name": "Ohio", "url": "https://businesssearch.ohiosos.gov"},
    "OK": {"name": "Oklahoma", "url": "https://sos.ok.gov/corp/corpInquiryFind.aspx"},
    "OR": {"name": "Oregon", "url": "https://egov.sos.state.or.us/br/pkg_web_name_srch_inq.login"},
    "PA": {"name": "Pennsylvania", "url": "https://file.dos.pa.gov/search/business"},
    "RI": {"name": "Rhode Island", "url": "https://business.sos.ri.gov/corpweb/corpsearch/corpsearch.aspx"},
    "SC": {"name": "South Carolina", "url": "https://businessfilings.sc.gov/BusinessFiling/Entity/Search"},
    "SD": {"name": "South Dakota", "url": "https://sosenterprise.sd.gov/BusinessServices/Business/FilingSearch.aspx"},
    "TN": {"name": "Tennessee", "url": "https://tncab.tnsos.gov/business-entity-search"},
    "TX": {"name": "Texas", "url": "https://comptroller.texas.gov/taxes/franchise/account-status/search"},
    "UT": {"name": "Utah", "url": "https://businessregistration.utah.gov/EntitySearch/OnlineEntitySearch"},
    "VT": {"name": "Vermont", "url": "https://bizfilings.vermont.gov/business/businesssearch"},
    "VA": {"name": "Virginia", "url": "https://cis.scc.virginia.gov/EntitySearch/Index"},
    "WA": {"name": "Washington", "url": "https://ccfs.sos.wa.gov/#/AdvancedSearch"},
    "WV": {"name": "West Virginia", "url": "https://apps.wv.gov/sos/businessentitysearch/"},
    "WI": {"name": "Wisconsin", "url": "https://apps.dfi.wi.gov/apps/corpsearch/Advanced.aspx"},
    "WY": {"name": "Wyoming", "url": "https://wyobiz.wyo.gov/Business/FilingSearch.aspx"},
    "DC": {"name": "District of Columbia", "url": "https://corponline.dlcp.dc.gov/homepage/business-search"},
}

# ── Statutory Agent Services (skip for person search) ─────────
STATUTORY_AGENTS = [
    "CT Corporation", "CT Corp", "C T Corporation", "Northwest Registered Agent",
    "Cogency Global", "Corporation Service Company", "CSC", "Registered Agents Inc",
    "United States Corporation Agents", "National Registered Agents", "NRAI",
    "Incorp Services", "The Corporation Trust", "Paracorp", "Capitol Corporate Services",
    "ZenBusiness", "LegalZoom", "Rocket Lawyer", "Wolters Kluwer", "Vcorp Services",
    "Legalinc", "Harvard Business Services", "Sundoc Filings", "BizFilings",
    "MyCorporation", "Spiegel & Utrera", "United Agent Group",
]

# ── Business Name Suffixes (for person vs company classification) ──
BIZ_SUFFIXES = {
    "LLC", "L.L.C.", "INC", "INC.", "CORP", "CORP.", "CORPORATION",
    "LLP", "L.L.P.", "LP", "L.P.", "LTD", "LTD.", "LIMITED",
    "PLLC", "P.L.L.C.", "PA", "P.A.", "PC", "P.C.",
    "CO", "CO.", "COMPANY", "ENTERPRISES", "HOLDINGS",
    "GROUP", "PARTNERS", "SERVICES", "ASSOCIATES",
}


def is_statutory(name: str) -> bool:
    lower = name.lower()
    return any(sa.lower() in lower for sa in STATUTORY_AGENTS)
