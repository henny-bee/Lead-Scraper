"""Non-environment limits and tunables."""

from __future__ import annotations

from pathlib import Path

# --- Filesystem layout -------------------------------------------------------------------------
#: Repository / image root (``src/leadscraper/constants.py`` -> parents[2]).
_SRC_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT: Path = _SRC_ROOT if (_SRC_ROOT / "config").is_dir() else Path.cwd()
CONFIG_DIR: Path = PROJECT_ROOT / "config"
DATA_DIR: Path = PROJECT_ROOT / "data"

# --- Request limits ----------------------------------------------------------------------------
MAX_OUTPUT_DEFAULT = 100
MAX_OUTPUT_UPPER = 5000              # "1–5000 (upper bound configurable)"
MAX_REGIONS = 50
MIN_INDUSTRIES = 0                   # industries are optional: empty = all companies (ANY_INDUSTRY_*)
#: Request without industries: the OSM filters for "any company" (value "*" = the key exists).
#: Web-search discovery needs industry keywords, so it does not run for such slices.
ANY_INDUSTRY_ID = "any"
ANY_INDUSTRY_OSM_TAGS = (("office", "*"), ("craft", "*"), ("industrial", "*"), ("man_made", "works"))
MAX_INDUSTRIES = 20
FRESHNESS_DAYS_DEFAULT = 90
FRESHNESS_DAYS_MAX = 365
WAIT_MAX_SECONDS = 20                # POST /scrape?wait=N, N <= 20
VERIFY_SYNC_LIMIT = 50               # POST /verify: <= 50 emails processed synchronously
VERIFY_BATCH_LIMIT = 10_000          # POST /verify: > 50 and <= 10 000 -> async job; more -> 422
RESULT_PAGE_LIMIT_MAX = 5000         # GET /scrape/{id}?limit=... upper bound
META_INDUSTRY_LIMIT = 20             # GET /meta/industries result size

# --- Job lifecycle -----------------------------------------------------------------------------
SCRAPE_JOB_PREFIX = "scr_"
VERIFY_JOB_PREFIX = "vrf_"
JOB_MAX_RUNTIME_MINUTES = 120        # running longer -> cancelled, failed with `job_timeout`
JOB_TIMEOUT_ERROR_CODE = "job_timeout"
#: DELETE / callback 2xx leave a data-free tombstone that expires after this many minutes.
TOMBSTONE_TTL_MINUTES: float | None = None
CLEANUP_SWEEP_INTERVAL_S = 30.0      # how often the TTL sweeper runs
CANDIDATES_FILE = "candidates.jsonl"
CRAWL_DIR = "crawl"
RESULT_FILE = "result.json"

# --- Callback -----------------------------------------------------------------------------------
CALLBACK_TIMEOUT_S = 10.0            # single attempt; retry callback is v0.4

# --- API protection -----------------------------------------------------------------------------
RATE_LIMIT_REQUESTS_PER_MINUTE = 120  # per client IP, in-process
RATE_LIMIT_BURST = 30
RATE_LIMIT_MAX_TRACKED_IPS = 10_000   # bounded limiter state (idle IPs are evicted)
RATE_LIMIT_IDLE_EVICT_S = 600.0

# --- Overpass politeness ------------------------------------------------------------------------
#: Requests in flight per Overpass endpoint.: overpass-api.de/api/status reports "Rate limit: 2"
#: slots per IP (it was 1 per process).
OVERPASS_MAX_CONCURRENCY = 2
#: failover endpoints, used only when OVERPASS_URL is the public default (a self-hosted instance
#: never leaks queries), only on 429/5xx/timeout/runtime error; set to () to disable.
OVERPASS_MIRROR_URLS: tuple[str, ...] = ("https://overpass.kumi.systems/api/interpreter",
                                         "https://overpass.private.coffee/api/interpreter")
OVERPASS_MIRROR_CONNECT_TIMEOUT_S = 5.0
OVERPASS_MAX_REQUESTS_PER_MINUTE = 10
OVERPASS_REQUEST_BUDGET_PER_JOB = 100
OVERPASS_QUERY_TIMEOUT_S = 180
#: Client read timeout for Overpass requests; must exceed the server-side `[timeout:N]` so a slow
#: query ends with Overpass's own `runtime error` remark instead of a client timeout.
OVERPASS_HTTP_TIMEOUT_S = OVERPASS_QUERY_TIMEOUT_S + 60.0
#: Fixed safety cap on elements per Overpass response (`out tags center <cap>`), independent of the
#: slice quota; the pipeline stops consuming once its target is reached.
OVERPASS_MAX_ELEMENTS_PER_QUERY = MAX_OUTPUT_UPPER
OVERPASS_DAILY_BUDGET = 5000         # in-process daily cap
OVERPASS_RETRY_ATTEMPTS = 3          # on 429/502/503/504/timeout (tenacity), then slice exhausted
OVERPASS_RETRY_WAIT_S = 10.0         # exponential backoff base ...
OVERPASS_RETRY_WAIT_MAX_S = 60.0     # ... capped
#: Keywords shorter than this must match a whole word in the Overpass name regex (so `werk` does not
#: match Handwerk/Werkstatt, `lager` not Bierlager); longer ones match as substrings (compounds).
OVERPASS_SUBSTRING_MIN_LEN = 8

# --- Planner & dedup ----------------------------------------------------------------------------
#: Discovery over-fetch factor per country tier.
OVERFETCH_FACTOR_BY_TIER: dict[str, float] = {"A": 1.5, "B": 1.5, "C": 2.0}
OVERFETCH_FACTOR_DEFAULT = 2.0
DEDUP_NAME_THRESHOLD = 92            # RapidFuzz token_set_ratio for name + postal code dedup
#: a country-wide request (``regions: []``) is planned as one slice per first-level ISO 3166-2
#: subdivision when there are 1…this many; otherwise one country slice.
COUNTRY_SPLIT_MAX_SUBDIVISIONS = 40
#: parent-less pycountry "subdivisions" that are territories with their own ISO 3166-1 code (the
#: resolver treats them as separate countries); dropped before the cap check.
COUNTRY_SPLIT_EXCLUDED_CODES: frozenset[str] = frozenset({
    "NL-AW", "NL-CW", "NL-SX", "FR-BL", "FR-MF", "FR-NC", "FR-PF", "FR-PM", "FR-TF", "FR-WF",
    "CN-HK", "CN-MO", "CN-TW", "US-AS", "US-GU", "US-MP", "US-PR", "US-UM", "US-VI"})
#: top-up rounds stop after this many × ``max_output`` processed candidates.
TOPUP_MAX_PROCESSED_FACTOR = 10
#: identity check for looked-up websites — distinctive-name score needed with location evidence, and
#: the stricter score for a name-only match (no postcode/city known).
IDENTITY_NAME_MIN_SCORE = 85
IDENTITY_NAME_ONLY_MIN_SCORE = 95
#: search-based website lookup — at most this many plausible result domains are crawled per
#: candidate; fuzzy name-vs-domain-label score for plausibility.
LOOKUP_MAX_TRIES = 2
LOOKUP_DOMAIN_MIN_SCORE = 80
#: guess the website from the name (DNS first, identity with location evidence required); at most
#: this many guessed domains; names whose joined slug is shorter than GUESS_MIN_NAME_LEN are not
#: guessed.
WEBSITE_GUESS_ENABLED = True
GUESS_MAX_DOMAINS = 3
GUESS_MIN_NAME_LEN = 6
#: OSM ``place`` types whose names make up an area gazetteer (the city rule of the source-agnostic
#: region check); also the levels that count as a city-level area.
GAZETTEER_PLACE_TYPES = ("city", "town", "village", "suburb", "hamlet")
#: a homepage with less visible text than this counts as a JavaScript app shell under the
#: noscript/empty-markup rules (crawler/js_shell.py).
JS_SHELL_TEXT_MAX = 200

# --- Website resolver ---------------------------------------------------------------------------
#: Social-media / directory / link-shortener sites: a candidate whose only website is one of these
#: is treated as "no website".
NON_COMPANY_SITE_DOMAINS: frozenset[str] = frozenset({
    "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "xing.com", "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "pinterest.",
    "whatsapp.com", "wa.me", "t.me", "telegram.me", "threads.net", "vk.com", "google.",
    "goo.gl", "g.page", "g.co", "maps.app.goo.gl", "yelp.", "tripadvisor.", "foursquare.com",
    "gelbeseiten.de", "dasoertliche.de", "dastelefonbuch.de", "11880.com", "wlw.de",
    "europages.", "kompass.com", "pagesjaunes.fr", "yell.com", "linktr.ee", "bit.ly", "tinyurl.com",
    "booking.com", "airbnb.", "wikipedia.org",
    # DE/EU business directories and job/review sites (search results are filtered with this list
    # before any crawl)
    "northdata.", "firmenwissen.de", "cylex.", "kununu.com", "stepstone.", "indeed.", "meinestadt.de",
    "branchenbuch.", "goyellow.de", "companyhouse.de", "bundesanzeiger.de", "handelsregister.de",
    "unternehmensregister.de", "implisense.com", "firmania.", "moneyhouse.", "infobel.", "hotfrog.",
    "golocal.de", "werliefertwas.de", "trustpilot.", "glassdoor.",
})
TRACKING_QUERY_PREFIXES: tuple[str, ...] = ("utm_", "mc_", "pk_", "hsa_")
TRACKING_QUERY_KEYS: frozenset[str] = frozenset({
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid", "_ga", "_gl", "ref", "ref_src"})
MAX_REDIRECTS = 5                    # redirect hops followed
WEBSITE_CONFIRM_TIMEOUT_S = 15.0

# --- Web search ---------------------------------------------------------------------------------------
#: DuckDuckGo HTML endpoint — the WEB_SEARCH_URL default (settings.py repeats the literal; a test
#: checks both are equal).
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
WEB_SEARCH_OFF = "off"                     # WEB_SEARCH_URL=off (case-insensitive) disables search
WEB_SEARCH_MIN_INTERVAL_S = 3.0            # process-wide spacing between two search requests
WEB_SEARCH_BUDGET_PER_JOB = 60             # search requests per job (lookups + discovery)
WEB_SEARCH_DAILY_BUDGET = 1000             # in-process daily cap
WEB_SEARCH_COOLDOWN_S = 900.0              # breaker / unreachable robots.txt: search paused this long
WEB_SEARCH_TIMEOUT_S = 10.0
WEB_SEARCH_MAX_RESULTS = 10                # results kept per query
#: web-search discovery queries per slice ("<keyword> <area>"; the first concurrently with Overpass,
#: the rest only in top-up rounds) and the share of WEB_SEARCH_BUDGET_PER_JOB discovery may use.
WEB_DISCOVERY_QUERIES_PER_SLICE = 3
WEB_DISCOVERY_BUDGET_FRACTION = 1 / 3

# --- Crawler — env-configurable values are in settings (CRAWLER_*) ------------------------------
CRAWLER_HTTP_TIMEOUT_S = 20.0        # overall/pool/write timeout; robots.txt keeps it as read timeout
#: companies in flight = factor × CRAWLER_GLOBAL_CONCURRENCY (connections in flight stay
#: CRAWLER_GLOBAL_CONCURRENCY; per-domain lock and delay unchanged).
CRAWLER_COMPANY_CONCURRENCY_FACTOR = 4
CRAWLER_CONNECT_TIMEOUT_S = 5.0      # page requests: connect ...
CRAWLER_READ_TIMEOUT_S = 10.0        # ... and read timeout
CRAWL_SITE_BUDGET_S = 45.0           # wall-clock budget per company website
# GET /contacts and POST /websites/find
CONTACTS_DEEP_MAX_PAGES = 20         # mode=deep: pages per site (robots + per-domain delay still apply)
CONTACTS_DEEP_BUDGET_S = 120.0       # mode=deep: wall-clock budget (20 pages x 2 s delay = 40 s+)
CONTACTS_DEFAULT_COUNTRY = "DE"      # phone/keyword region when neither `country` nor a ccTLD says
FIND_WEBSITE_SEARCH_BUDGET = 2       # search requests per POST /websites/find
CRAWL_MAX_CONSECUTIVE_FAILURES = 2   # stop a site after this many network errors/timeouts in a row
ROBOTS_MAX_CRAWL_DELAY_S = 10.0      # honour robots.txt Crawl-delay up to this cap
ROBOTS_MAX_BYTES = 512 * 1024        # robots.txt larger than this is truncated (RFC 9309: ≥500 KiB)
HTML_CONTENT_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})
#: sitemap fallback — at most this many <loc> entries are read, and at most this many child sitemaps
#: of a sitemap index are fetched (every fetch counts toward the page budget).
SITEMAP_MAX_LOCS = 1000
SITEMAP_MAX_CHILDREN = 1

# --- Extraction --------------------------------------------------------------------------------
#: schema.org types read from JSON-LD. Any other type whose name ends in "Business" or
#: "Organization" is accepted too (schema.org subtype naming).
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

# --- Resolver -----------------------------------------------------------------------------------
NOMINATIM_CACHE_MAXSIZE = 1024       # bounded process-lifetime geodata cache (no job data)
NOMINATIM_HTTP_TIMEOUT_S = 10.0
NOMINATIM_ACCEPT_SCORE = 90          # fuzz.ratio(query, result name) needed to accept a hit (C13)
INDUSTRY_SEARCH_MIN_SCORE = 60       # /meta/industries autocomplete cutoff (WRatio)
INDUSTRY_SUGGESTION_LIMIT = 3        # nearest ISIC titles in an unresolved_industry 422

# --- Verification ------------------------------------------------------------------------------
VERIFICATION_CONFIG_FILE = CONFIG_DIR / "verification.yaml"
SUPPRESSION_FILE = CONFIG_DIR / "suppression.txt"
DNS_TIMEOUT_S = 4.0                  # per nameserver attempt
DNS_LIFETIME_S = 8.0                 # total per query
SMTP_MAX_CONNECTIONS_PER_MX = 2      # 1–2 parallel connections per MX host

# --- Verification -------------------------------------------------------------------------------
SMTP_GREYLIST_BACKOFF_MINUTES: tuple[int, ...] = (5, 15, 60)  # async /verify jobs only
SMTP_TIMEOUT_S = 15.0
SMTP_PORT = 25
