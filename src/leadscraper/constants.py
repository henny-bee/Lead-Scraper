"""Non-environment limits and tunables (PLAN.md Q3, Q4, Q5, Q9, Q19; Supervisor 2026-09-24).

ARCHITECTURE.md §9 is the exact env contract, so everything here is a module constant — never an
env var, and never an inline literal in a route handler.
"""

from __future__ import annotations

from pathlib import Path

# --- Filesystem layout -------------------------------------------------------------------------
#: Repository / image root (``src/leadscraper/constants.py`` -> parents[2]). In Docker the project is
#: installed editable from ``/app/src``, so this resolves to ``/app`` (A§10.1 copies ``config`` there).
#: Falls back to the working directory (``WORKDIR /app``) for a non-editable install.
_SRC_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT: Path = _SRC_ROOT if (_SRC_ROOT / "config").is_dir() else Path.cwd()
CONFIG_DIR: Path = PROJECT_ROOT / "config"
DATA_DIR: Path = PROJECT_ROOT / "data"

# --- Request limits (A§2.1, A§2.4; Q19) --------------------------------------------------------
MAX_OUTPUT_DEFAULT = 100
MAX_OUTPUT_UPPER = 5000              # "1–5000 (upper bound configurable)"
MAX_REGIONS = 50
MIN_INDUSTRIES = 1
MAX_INDUSTRIES = 20
FRESHNESS_DAYS_DEFAULT = 90
FRESHNESS_DAYS_MAX = 365
WAIT_MAX_SECONDS = 20                # POST /scrape?wait=N, N <= 20
VERIFY_SYNC_LIMIT = 50               # POST /verify: <= 50 emails processed synchronously
VERIFY_BATCH_LIMIT = 10_000          # POST /verify: > 50 and <= 10 000 -> async job; more -> 422
RESULT_PAGE_LIMIT_MAX = 5000         # GET /scrape/{id}?limit=... upper bound
META_INDUSTRY_LIMIT = 20             # GET /meta/industries result size

# --- Job lifecycle (Q3, Q5, Q20) ---------------------------------------------------------------
SCRAPE_JOB_PREFIX = "scr_"
VERIFY_JOB_PREFIX = "vrf_"
JOB_MAX_RUNTIME_MINUTES = 120        # Q5: running longer -> cancelled, failed with `job_timeout`
JOB_TIMEOUT_ERROR_CODE = "job_timeout"
#: Q3: DELETE / callback 2xx leave a data-free tombstone that expires after this many minutes.
#: ``None`` means "use settings.JOB_TTL_MINUTES" (tombstone TTL = JOB_TTL_MINUTES, Supervisor).
TOMBSTONE_TTL_MINUTES: float | None = None
CLEANUP_SWEEP_INTERVAL_S = 30.0      # how often the TTL sweeper runs
CANDIDATES_FILE = "candidates.jsonl"
CRAWL_DIR = "crawl"
RESULT_FILE = "result.json"

# --- Callback (Q4) ------------------------------------------------------------------------------
CALLBACK_TIMEOUT_S = 10.0            # single attempt; retry callback is v0.4 (A§12)

# --- API protection (T23) -----------------------------------------------------------------------
RATE_LIMIT_REQUESTS_PER_MINUTE = 120  # per client IP, in-process
RATE_LIMIT_BURST = 30
RATE_LIMIT_MAX_TRACKED_IPS = 10_000   # bounded limiter state (idle IPs are evicted)
RATE_LIMIT_IDLE_EVICT_S = 600.0

# --- Overpass politeness (T11; A§1, A§4) --------------------------------------------------------
OVERPASS_MAX_CONCURRENCY = 1         # at most one concurrent Overpass request per process
OVERPASS_MAX_REQUESTS_PER_MINUTE = 10
OVERPASS_REQUEST_BUDGET_PER_JOB = 100
OVERPASS_QUERY_TIMEOUT_S = 180       # `[timeout:180]` (A§4)
#: Client read timeout for Overpass requests; must exceed the server-side `[timeout:N]` so a slow
#: query ends with Overpass's own `runtime error` remark instead of a client timeout. Applied per
#: request, so it also holds when the adapter shares the crawler's httpx client (20 s timeout).
OVERPASS_HTTP_TIMEOUT_S = OVERPASS_QUERY_TIMEOUT_S + 60.0
#: Fixed safety cap on elements per Overpass response (`out tags center <cap>`), independent of the
#: slice quota; the pipeline stops consuming once its target is reached. A response with >= cap
#: elements was truncated → slice reason `saturated` + tiles_saturated_total (A§11).
OVERPASS_MAX_ELEMENTS_PER_QUERY = MAX_OUTPUT_UPPER
OVERPASS_DAILY_BUDGET = 5000         # in-process daily cap (public instance: < 10 000 queries/day, A§4)
OVERPASS_RETRY_ATTEMPTS = 3          # on 429/502/503/504/timeout (tenacity), then slice exhausted
OVERPASS_RETRY_WAIT_S = 10.0         # exponential backoff base ...
OVERPASS_RETRY_WAIT_MAX_S = 60.0     # ... capped
#: Keywords shorter than this must match a whole word in the Overpass name regex (so `werk` does not
#: match Handwerk/Werkstatt, `lager` not Bierlager); longer ones match as substrings (compounds).
OVERPASS_SUBSTRING_MIN_LEN = 8

# --- Planner & dedup (T12; A§3.5, A§3.8) --------------------------------------------------------
#: Discovery over-fetch factor per country tier (A§2.1: "1.5–2×", later calibrated from
#: email_found_ratio). Tier C has lower email yield (A§4), so it gets the upper bound.
OVERFETCH_FACTOR_BY_TIER: dict[str, float] = {"A": 1.5, "B": 1.5, "C": 2.0}
OVERFETCH_FACTOR_DEFAULT = 2.0
DEDUP_NAME_THRESHOLD = 92            # RapidFuzz token_set_ratio for name + postal code dedup (A§3.8)

# --- Website resolver (T13; Q21, Q22) -----------------------------------------------------------
#: Social-media / directory / link-shortener sites: a candidate whose only website is one of these
#: is treated as "no website" (legal-notice-first needs the company's own site, Q22).
#: Matched against the registered domain; entries ending in "." match any public suffix.
NON_COMPANY_SITE_DOMAINS: frozenset[str] = frozenset({
    "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "xing.com", "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "pinterest.",
    "whatsapp.com", "wa.me", "t.me", "telegram.me", "threads.net", "vk.com", "google.",
    "goo.gl", "g.page", "g.co", "maps.app.goo.gl", "yelp.", "tripadvisor.", "foursquare.com",
    "gelbeseiten.de", "dasoertliche.de", "dastelefonbuch.de", "11880.com", "wlw.de",
    "europages.", "kompass.com", "pagesjaunes.fr", "yell.com", "linktr.ee", "bit.ly", "tinyurl.com",
    "booking.com", "airbnb.", "wikipedia.org",
})
TRACKING_QUERY_PREFIXES: tuple[str, ...] = ("utm_", "mc_", "pk_", "hsa_")
TRACKING_QUERY_KEYS: frozenset[str] = frozenset({
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid", "_ga", "_gl", "ref", "ref_src"})
MAX_REDIRECTS = 5                    # redirect hops followed (each hop SSRF-checked, Q21)
WEBSITE_CONFIRM_TIMEOUT_S = 15.0

# --- Crawler (T14; A§3.6) — env-configurable values are in settings (CRAWLER_*) -----------------
CRAWLER_HTTP_TIMEOUT_S = 20.0
ROBOTS_MAX_CRAWL_DELAY_S = 10.0      # honour robots.txt Crawl-delay up to this cap
ROBOTS_MAX_BYTES = 512 * 1024        # robots.txt larger than this is truncated (RFC 9309: ≥500 KiB)
HTML_CONTENT_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})

# --- Extraction (T15) --------------------------------------------------------------------------
#: schema.org types read from JSON-LD (A§3.6: Organization / LocalBusiness). Any other type whose
#: name ends in "Business" or "Organization" is accepted too (schema.org subtype naming).
JSONLD_ORG_TYPES: frozenset[str] = frozenset({
    "Organization", "Corporation", "LocalBusiness", "ProfessionalService", "Store",
    "AutoDealer", "AutoRepair", "Restaurant", "FoodEstablishment", "Hotel", "LodgingBusiness",
    "GeneralContractor", "HomeAndConstructionBusiness", "MovingCompany", "NGO",
    "OnlineBusiness", "WholesaleStore", "Manufacturer"})
JSONLD_MAX_BLOCKS = 20               # per page
JSONLD_MAX_DEPTH = 6                 # nested objects walked per block
LEGAL_NAME_MAX_WORDS = 10            # longer "names" before a legal form are sentences, not names
LEGAL_NAME_MAX_LINE = 140
LEGAL_NAME_SOURCE_MATCH_MIN = 60     # token_set_ratio to prefer the legal name matching the source name
ADDRESS_MAX_LINE = 90

# --- Resolver (Q7) ------------------------------------------------------------------------------
NOMINATIM_CACHE_MAXSIZE = 1024       # bounded process-lifetime geodata cache (no job data)
NOMINATIM_HTTP_TIMEOUT_S = 10.0
NOMINATIM_ACCEPT_SCORE = 90          # fuzz.ratio(query, result name) needed to accept a hit (C13)
INDUSTRY_SEARCH_MIN_SCORE = 60       # /meta/industries autocomplete cutoff (WRatio)
INDUSTRY_SUGGESTION_LIMIT = 3        # nearest ISIC titles in an unresolved_industry 422

# --- Verification (T18; A§5.1) -----------------------------------------------------------------
VERIFICATION_CONFIG_FILE = CONFIG_DIR / "verification.yaml"
SUPPRESSION_FILE = CONFIG_DIR / "suppression.txt"
DNS_TIMEOUT_S = 4.0                  # per nameserver attempt
DNS_LIFETIME_S = 8.0                 # total per query
SMTP_MAX_CONNECTIONS_PER_MX = 2      # A§5.2: 1–2 parallel connections per MX host

# --- Verification (Q9) --------------------------------------------------------------------------
SMTP_GREYLIST_BACKOFF_MINUTES: tuple[int, ...] = (5, 15, 60)  # async /verify jobs only
SMTP_TIMEOUT_S = 15.0
SMTP_PORT = 25
