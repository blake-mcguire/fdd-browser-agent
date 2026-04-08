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

# ── Browser ──────────────────────────────────────────────────
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() in ("true", "1", "yes")

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
COMPANY_MAX_STEPS = int(os.getenv("COMPANY_MAX_STEPS", "10"))
PERSON_MAX_STEPS = int(os.getenv("PERSON_MAX_STEPS", "12"))

# ── Output ────────────────────────────────────────────────────
MAX_CONTACTS = int(os.getenv("MAX_CONTACTS", "8"))

# ── SOS URL Registry ─────────────────────────────────────────
SOS_REGISTRY = {
    "AL": {"name": "Alabama", "url": "https://arc-sos.state.al.us/cgi/corpname.mbr"},
    "AK": {"name": "Alaska", "url": "https://commerce.alaska.gov/cbp/main/search/entities"},
    "AZ": {"name": "Arizona", "url": "https://ecorp.azcc.gov/EntitySearch/Index"},
    "AR": {"name": "Arkansas", "url": "https://sos-corp-search.ark.org/corps"},
    "CA": {"name": "California", "url": "https://bizfileonline.sos.ca.gov/search/business"},
    "CO": {"name": "Colorado", "url": "https://sos.state.co.us/biz/BusinessEntityCriteriaExt.do"},
    "CT": {"name": "Connecticut", "url": "https://service.ct.gov/business/s/onlinebusinesssearch"},
    "DE": {"name": "Delaware", "url": "https://icis.corp.delaware.gov/ecorp/entitysearch/namesearch.aspx"},
    "FL": {"name": "Florida", "url": "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"},
    "GA": {"name": "Georgia", "url": "https://ecorp.sos.ga.gov/BusinessSearch"},
    "HI": {"name": "Hawaii", "url": "https://hbe.ehawaii.gov/documents/search.html"},
    "ID": {"name": "Idaho", "url": "https://sosbiz.idaho.gov/search/business"},
    "IL": {"name": "Illinois", "url": "https://apps.ilsos.gov/corporatellc"},
    "IN": {"name": "Indiana", "url": "https://bsd.sos.in.gov/publicbusinesssearch"},
    "IA": {"name": "Iowa", "url": "https://sos.iowa.gov/search/business"},
    "KS": {"name": "Kansas", "url": "https://sos.ks.gov/eforms/BusinessEntity/Search.aspx"},
    "KY": {"name": "Kentucky", "url": "https://sosbes.sos.ky.gov/BusSearchNProfile/search.aspx"},
    "LA": {"name": "Louisiana", "url": "https://coraweb.sos.la.gov/CommercialSearch/CommercialSearch.aspx"},
    "ME": {"name": "Maine", "url": "https://icrs.informe.org/nei-sos-icrs/ICRS?MainPage=x"},
    "MD": {"name": "Maryland", "url": "https://egov.maryland.gov/BusinessExpress/EntitySearch"},
    "MA": {"name": "Massachusetts", "url": "https://corp.sec.state.ma.us/corpweb/CorpSearch/CorpSearch.aspx"},
    "MI": {"name": "Michigan", "url": "https://cofs.lara.state.mi.us/CorpWeb/CorpSearch/CorpSearch.aspx"},
    "MN": {"name": "Minnesota", "url": "https://mblsportal.sos.mn.gov/Business/Search"},
    "MS": {"name": "Mississippi", "url": "https://corp.sos.ms.gov/corp/portal/c/page/corpBusinessNameSearch"},
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
    "RI": {"name": "Rhode Island", "url": "https://business.sos.ri.gov/CorpWeb/CorpSearch/CorpSearch.aspx"},
    "SC": {"name": "South Carolina", "url": "https://businessfilings.sc.gov/BusinessFiling/Entity/Search"},
    "SD": {"name": "South Dakota", "url": "https://sosenterprise.sd.gov/BusinessServices/Business/FilingSearch.aspx"},
    "TN": {"name": "Tennessee", "url": "https://tnbear.tn.gov/ECommerce/FilingSearch.aspx"},
    "TX": {"name": "Texas", "url": "https://comptroller.texas.gov/taxes/franchise/account-status/search"},
    "UT": {"name": "Utah", "url": "https://businessregistration.utah.gov/"},
    "VT": {"name": "Vermont", "url": "https://bizfilings.vermont.gov/online/BusinessInquire"},
    "VA": {"name": "Virginia", "url": "https://cis.scc.virginia.gov/EntitySearch"},
    "WA": {"name": "Washington", "url": "https://ccfs.sos.wa.gov"},
    "WV": {"name": "West Virginia", "url": "https://apps.sos.wv.gov/business/corporations"},
    "WI": {"name": "Wisconsin", "url": "https://apps.wi.gov/CorpSearch"},
    "WY": {"name": "Wyoming", "url": "https://wyobiz.wyo.gov/Business/FilingSearch.aspx"},
    "DC": {"name": "District of Columbia", "url": "https://corponline.dcra.dc.gov/BizEntity.aspx"},
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
