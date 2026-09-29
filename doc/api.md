# API reference

Base URL `http://localhost:8000`. JSON in, JSON out, except for the CSV export. Interactive
OpenAPI docs are at `/docs`.

Authentication is **off by default**. If `API_KEY` is set, every endpoint except `/health`
needs `X-API-Key: <key>` or `Authorization: Bearer <key>`.

Rate limiting: every endpoint except `/health` and `/metrics` allows 120 requests/min per client
IP with a burst of 30.

The examples below were produced by the running service. Warning and error messages are shown
exactly as the service returns them.

## Error envelope

Every error uses the same shape:

```json
{
  "status": "error",
  "error": {
    "code": "unresolved_region",
    "message": "Region 'Bayerm' could not be resolved for country DE",
    "details": { "input": "Bayerm", "suggestions": [{ "id": "iso:DE-BY", "name": "Bayern" }] }
  }
}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `bad_request` | Body cannot be parsed at all, e.g. invalid UTF-8. Malformed JSON gives `422 validation_error` with type `json_invalid`. |
| 401 | `unauthorized` | `API_KEY` set, no key sent (`WWW-Authenticate: Bearer`) |
| 403 | `forbidden` | Wrong key |
| 404 | `not_found` | Unknown or expired job, or one already deleted (`DELETE`, callback 2xx) |
| 405 | `method_not_allowed` | Wrong HTTP method |
| 409 | `job_not_finished` | CSV export of a job that is not `success` yet |
| 422 | `validation_error` | Schema violation. `details.errors[]` holds `loc`, `msg`, `type`. |
| 422 | `unresolved_country` / `unresolved_region` / `unresolved_industry` | Input cannot be resolved with confidence. `details.input` and `details.suggestions` are included. Only the first failing value is reported, checked in the order country → regions → industries. |
| 422 | `unsupported_format` | Export `format` other than `csv` |
| 429 | `rate_limited` | Too many requests. `Retry-After` header and `details.retry_after_s` are included. |
| 500 | `internal_error` | Unexpected error |
| 503 | `not_ready` | `/health` only: resolver index not warm or `TEMP_DIR` not writable |

## Request body: `POST /scrape` and `POST /scrape/resolve`

| Field | Type | Rules |
|---|---|---|
| `country` | string, required | 2–100 characters. A country name in any language, or an ISO alpha-2/alpha-3 code (`Germany`, `Deutschland`, `Allemagne`, `DE`, `DEU`). |
| `regions` | string[] | 0–50 items. Empty means the whole country. ISO 3166-2 names or codes, aliases (`NRW`, `Bavaria`, `paca`), small typos. City level needs `NOMINATIM_URL`. |
| `industries` | string[], optional | 0–20 free-text values in any language, mapped to ISIC Rev.4. Omitted or empty = all companies in the area (OSM entries with an `office`, `craft` or `industrial` tag, or `man_made=works`); web-search discovery is skipped then, and a warning says so |
| `information` | enum[], required | One or more of `company_name`, `company_email`, `website`, `phone`, `address`, `legal_form`, `register_number`, `vat_id` |
| `max_output` | int | 1–5000, default 100 |
| `verify_emails` | bool | Default `false`. Syntax + DNS check; unusable emails are replaced or the company is dropped. It adds no fields to the output. |
| `exclude_marketing_objections` | bool | Default `true`. Drops companies whose legal/contact page objects to advertising email. |
| `freshness_days` | int | 1–365, default 90. Accepted for compatibility and ignored (there is no database). |
| `callback_url` | URL | Optional. The final body is POSTed here once. |

Values in `regions` and `industries` are trimmed and de-duplicated case-insensitively.

## `POST /scrape/resolve`

This is a dry run: it shows how the input is interpreted, and nothing is scraped.

```json
{
  "country": "United States",
  "regions": ["California", "Texas"],
  "industries": ["Manufacturing", "Logistics"],
  "information": ["company_name", "company_email", "website"],
  "max_output": 500
}
```

`200`:

```json
{
  "status": "success",
  "resolved": {
    "country": { "input": "United States", "code": "US", "languages": ["en"], "tier": "C" },
    "regions": [
      { "input": "California", "id": "iso:US-CA", "name": "California", "level": "state", "method": "exact" },
      { "input": "Texas", "id": "iso:US-TX", "name": "Texas", "level": "state", "method": "exact" }
    ],
    "industries": [
      { "input": "Manufacturing", "isic": ["C"], "scheme": "ISIC", "version": "Rev.4",
        "keywords": { "en": ["manufacturing", "factory", "manufacturer", "works"] }, "method": "catalog" },
      { "input": "Logistics", "isic": ["49", "52", "53"], "scheme": "ISIC", "version": "Rev.4",
        "keywords": { "en": ["logistics", "freight", "forwarding", "warehouse"] }, "method": "catalog" }
    ],
    "sources": ["osm", "web_search"],
    "known_in_job": 0,
    "compliance_note": "US: Check local data protection and outreach rules before using the data for marketing.",
    "warnings": ["Tier C: business websites are not required to publish a legal notice, so the email yield is expected to be lower."]
  }
}
```

What the fields mean:
- **`method`**
  - For regions: `exact`, `alias`, `fuzzy` (with a warning), or `country` when `regions` is empty.
  - For industries: `catalog`, `alias` or `fuzzy`; `any` when the request has no industries.
- **`tier`**
  - A: an open company register exists (GB, FR, NO).
  - B: EU/EEA/CH, where a legal notice is mandatory.
  - C: everything else.

  The tier is informational only: it tells you what email yield to expect.
- **`sources`** lists only the adapters that are enabled: `["osm", "web_search"]` by default,
  `["osm"]` with `WEB_SEARCH_URL=off`.
- **`warnings`** include fuzzy corrections, tier notes, and fields that may be unavailable for the
  country (such fields are returned as `null`). While a job runs, warnings about its sources are
  appended to `resolved.warnings` (visible in the `running` body of `GET /scrape/{job_id}`), for
  example:
  - `Overpass unavailable for slice NRW × Maschinenbau (http 504 at overpass-api.de); slice skipped.`
  - `Overpass query for slice NRW × Maschinenbau failed/timed out on the server (overpass.kumi.systems); slice skipped.`
  - `Overpass request budget exhausted for slice NRW × Maschinenbau; results may be fewer than max_output.`
  - `Overpass area gazetteer unavailable (request budget exhausted); the region check for non-OSM candidates uses OSM postcodes and area names only.`
  - `Web search budget exhausted for this job; results may be fewer than max_output.`
  - `Web search is paused after the search backend blocked requests; results may be fewer than max_output.`
  - `Web search is paused: the search backend's robots.txt could not be read.`
  - `Daily web search limit reached; results may be fewer than max_output.`

An unresolvable industry gives `422 unresolved_industry`. Its `details.suggestions` hold the
nearest ISIC titles, e.g. `{"isic": "47", "title": "Retail trade, except of motor vehicles and motorcycles"}`.

## `POST /scrape`

It takes the same body as above and resolves it first. Invalid input returns `422` before any job
is created. The optional `callback_url` field is described in the request-body table.

Query: `wait` = 0–20 seconds (default 0).

`202`:

```json
{ "status": "queued", "job_id": "scr_01JAB3K7Q9…", "poll_url": "/scrape/scr_01JAB3K7Q9…" }
```

- **Idempotent.** If you re-post an identical body while that job is still in RAM (and not
  failed/cancelled), you get the same `job_id`. `status` then shows the job's current state.
- **`?wait=N`.** If the job finishes within N seconds, you get `200` with the final body below.
  The job is kept, so you can GET or export it afterwards. If the job fails within N seconds, you
  get `200` with the failed body. Otherwise you get the `202` above.

## `GET /scrape/{job_id}`

Queued or running:

```json
{
  "status": "running",
  "job_id": "scr_01JAB3K7Q9…",
  "count": 312,
  "progress": { "target": 1000, "candidates": 1540, "crawled": 690, "with_email": 312 },
  "resolved": { "...": "same block as /scrape/resolve (null while queued)" }
}
```

`success`:

```json
{
  "status": "success",
  "job_id": "scr_01JAB3K7Q9…",
  "count": 847,
  "companies": [
    {
      "company_name": "Example Maschinenbau GmbH",
      "company_email": "info@example.de",
      "website": "https://example.de",
      "country": "Germany",
      "region": "Bayern",
      "industry": "Maschinenbau"
    }
  ]
}
```

How company objects are built:
- Each company contains **only** the requested `information` fields.
- It always contains `country`, `region` and `industry`. These are the labels exactly as you
  sent them: `NRW` stays `NRW`, and the canonical codes are in `resolved`.
- `region` is `null` when `regions` was empty.
- A field that could not be found is `null`.
- `phone` is in E.164 format.
- If `count < max_output`, the status is still `success`: the sources were exhausted or the
  per-job budget was reached.
- A company found by **web search** (not in OSM) is returned only when its legal notice (or
  JSON-LD) gives its legal name, which is the `company_name` (the search result title is never
  used), when its home or legal page mentions the industry, and when its own address is inside
  the requested area (see `doc/architecture.md` §2: a whole-country slice accepts only postcodes
  of the job's OSM companies; UK full postcodes match only those). If it is the same company as
  an OSM entry, one record is returned with the OSM entry's `region`/`industry` labels.

Other states:
- `{"status":"failed","job_id":"…","error":{"code":"…","message":"…","details":{}}}`, for example
  `job_timeout` after 120 minutes, or `internal_error`.
- `{"status":"cancelled","job_id":"…"}`

**Pagination.** `?offset=0&limit=500` (`limit` ≤ 5000) returns the final body with `companies`
sliced, plus `"offset"` and `"limit"` keys. `count` is always the total.

**Reading never deletes.** Remove a job with DELETE, or it expires by TTL (15 min after
finishing, 30 min after failing). The final GET (with or without pagination) can be repeated and
the result exported any number of times. A finished job is deleted only when:
- you call `DELETE /scrape/{job_id}`,
- its callback was answered with `2xx`, or
- its TTL runs out: `JOB_TTL_MINUTES` (15) for finished jobs, `JOB_FAILED_TTL_MINUTES` (30) for
  failed ones.

After that the id returns `404` on GET. `DELETE` still returns `204` while the tombstone lives
(`JOB_TTL_MINUTES`).

## `GET /scrape/{job_id}/export?format=csv`

Returns `text/csv; charset=utf-8` with `Content-Disposition: attachment; filename="<job_id>.csv"`.
The columns are the requested `information` fields followed by `country,region,industry`. Missing
values are empty cells.

- Exporting does not delete the job; you can export again or GET the JSON afterwards.
- A job that is not finished returns `409 job_not_finished`.
- `format=xlsx` returns `422 unsupported_format`.

## `DELETE /scrape/{job_id}`

Cancels the job if it is running, then deletes the job and its temp files. Returns `204`.

- It also returns `204` for an id that was already deleted (callback; tombstone).
- An unknown id returns `404`.
- A job cancelled this way never sends its callback.

## Callback

If `callback_url` was given, the finished job (`success`, `failed`, or `job_timeout`) POSTs
**the same body** as the final `GET`. There is a single attempt with a 10 s timeout, and the
request carries the configured User-Agent.

- **`2xx`:** the job is deleted immediately.
- **Anything else:** the job stays pollable until its TTL. There are no retries in v0.3.

Internal URLs such as `http://n8n:5678/webhook/lead-result` are allowed.

## `POST /verify`

```json
{ "emails": ["info@example.de", "vertrieb@example-logistik.de"], "smtp_check": true }
```

How the request size is handled:

| Emails in the request | Response |
|---|---|
| 1–50 | `200`, processed synchronously |
| 51–10 000 | `202`: `{"status":"queued","job_id":"vrf_…","poll_url":"/verify/vrf_…"}` |
| More than 10 000 | `422 validation_error` |

Checks run from cheapest to most expensive and stop once the result is certain:
1. syntax
2. suppression file
3. disposable domain
4. DNS (MX, with A/AAAA fallback and null-MX detection)
5. role/free-provider flags
6. SMTP (optional)

`200` (real output with SMTP disabled):

```json
{
  "status": "success",
  "count": 1,
  "results": [
    {
      "email": "not-an-email",
      "result": "undeliverable",
      "reason": "invalid_syntax",
      "score": 0.0,
      "verification_level": "syntax",
      "checks": { "syntax_valid": false },
      "cached": false,
      "checked_at": "2026-09-25T06:10:05Z"
    }
  ],
  "warnings": ["SMTP verification is not enabled in this deployment (SMTP_VERIFY_ENABLED / SMTP_HELO_HOST / SMTP_MAIL_FROM); results are at most DNS level (unknown)."]
}
```

A full result with SMTP looks like this:

```json
{ "email": "info@example.de", "result": "deliverable", "reason": "smtp_accepted", "score": 0.93,
  "verification_level": "smtp",
  "checks": { "syntax_valid": true, "domain_has_mx": true, "mx_hosts": ["mx01.example.de"],
              "is_disposable": false, "is_role_account": true, "is_free_provider": false,
              "is_catch_all": false, "smtp_code": 250 },
  "cached": false, "checked_at": "2026-09-23T09:14:02Z" }
```

About the fields:
- **`checks`** only contains checks that actually ran.
- **`cached`** is always `false`, because there is no cross-request cache.
- **`warnings`** only appears when there is something to warn about.

| `result` | Meaning |
|---|---|
| `deliverable` | The mailbox accepted `RCPT TO` and the domain is not catch-all |
| `undeliverable` | Bad syntax, no MX / null MX / NXDOMAIN, or SMTP 5xx rejection |
| `risky` | Catch-all or disposable domain |
| `unknown` | MX exists but no SMTP check was made, or a timeout, block or greylist |
| `suppressed` | Listed in `config/suppression.txt`. Nothing else is checked. |

`reason` values: `smtp_accepted`, `catch_all`, `disposable`, `smtp_not_checked`, `smtp_disabled`,
`a_record_fallback`, `greylisted`, `smtp_blocked`, `smtp_timeout`, `dns_error`, `invalid_syntax`,
`no_mx`, `null_mx`, `domain_not_found`, `smtp_rejected`.

`score` (0–1) comes from `config/verification.yaml`. `verification_level` is `syntax`, `dns` or
`smtp`.

**SMTP availability:**
- If `smtp_check` is `true` but SMTP is not configured, results are DNS-level `unknown`/
  `smtp_disabled` and the response includes the warning shown above.
- Synchronous requests make one SMTP attempt. Async jobs retry greylisting after 5, 15 and 60
  minutes.

## `GET /verify/{job_id}`

- **Running:**
  `{"status":"running","job_id":"vrf_…","count":120,"progress":{"target":500,"checked":120}}`
- **Finished:** the same body as the synchronous response, plus `job_id`. Reading never
  deletes it; it expires by TTL (15 min after finishing, 30 min after failing).
- **Failed or cancelled:** the same shapes as for scrape jobs.
- There is no `DELETE /verify/{id}`. Verify jobs expire by TTL.

## `GET /contacts`

Contact details of one website. Synchronous: the
crawl uses the same polite crawler as scrape jobs (robots.txt, 2 s per-domain delay, SSRF guard,
honest User-Agent), so it takes a few seconds up to about a minute (`deep`: up to 2 minutes).

`GET /contacts?website=acme-logistik.de[&mode=key_pages][&country=DE]`

| Parameter | Description |
|---|---|
| `website` | Required. Bare domain or full URL. Social-media and directory hosts are rejected |
| `mode` | `homepage` (1 page), `key_pages` (default: homepage + legal notice/contact pages, max 5), `deep` (up to 20 keyword-matching pages) |
| `country` | Optional. Region for phone parsing and page keywords. Default: the ccTLD (`.de` → DE), else DE |

Response (same keys for unreachable sites: empty lists and `error` holding the reason):

```json
{"domain": "acme-logistik.de", "title": "Acme Logistik GmbH – Spedition",
 "description": "Ihre Spedition in Bremen.",
 "emails": [{"value": "info@acme-logistik.de",
             "sources": ["https://acme-logistik.de/impressum", "https://acme-logistik.de/kontakt"],
             "is_likely_official": true}],
 "phones": ["+494211234567"],
 "linkedins": [{"value": "https://linkedin.com/company/acme-logistik",
                "sources": ["https://acme-logistik.de/"], "is_likely_official": true}],
 "twitters": [], "instagrams": [], "facebooks": [], "youtubes": [], "tiktoks": [],
 "pinterests": [], "discords": [], "snapchats": [], "threads": [], "telegrams": [],
 "reddits": [], "whatsapps": [], "githubs": [], "blueskys": [], "mediums": [], "calendlys": [],
 "error": null}
```

- `emails` are ranked best first. `is_likely_official` = on the site's own domain and not a
  special-function address (`datenschutz@`, `jobs@`, …). Suppressed addresses are never returned.
- Social profiles: the first entry per platform is flagged official when it is a real link or the
  only profile found. Share/login/content URLs and site-builder vendor profiles are dropped.
- Not included (unlike the hosted reference API): `technologies` and the AI `best_sales_email`.

## `POST /websites/find`

Finds a company's website from its name. No AI: candidates come from the email domain, web search
(`WEB_SEARCH_URL`) and domain guesses (DNS-checked), and each is confirmed by crawling it and
checking that the site names the company (and, if given, its location from the site's own legal
notice).

```json
{"name": "Acme Logistik GmbH", "country": "DE", "city": "Bremen", "postcode": "28195"}
```

`name` is required. Used context keys: `country`, `city`, `postcode` (`postal_code`, `zip`,
`plz`), `email`; other keys are accepted and ignored. Response:
`{"website": "https://acme-logistik.de", "method": "guess"}`, or `{"website": null, "method": null}`
when no candidate is confirmed. `method` is `email_domain`, `search` or `guess`.

## `GET /meta/*`

These endpoints use local data only.

- `GET /meta/countries` →
  `{"status":"success","count":249,"countries":[{"code":"AD","alpha_3":"AND","name":"Andorra"}, …]}`
- `GET /meta/regions?country=Deutschland` (free-text country; `422` if unknown) →
  `{"status":"success","country":"DE","count":16,"regions":[{"id":"iso:DE-BY","code":"DE-BY","name":"Bayern","level":"land","parent":null}, …]}`
- `GET /meta/industries?q=logistik` (up to 20 results) →
  `{"status":"success","count":5,"industries":[{"id":"logistics","isic":["49","52","53"],"title":"Land transport and transport via pipelines / …","match":"logistik"}, …],"scheme":"ISIC","version":"Rev.4"}`

## `GET /health`

- **Ready:** `200`, `{"status":"ok","checks":{"resolver_index":true,"temp_dir_writable":true}}`.
- **Not ready:** `503 not_ready`, with `details.checks` and `details.reasons`.
- It makes no network call, never needs the API key and is not rate-limited.

## `GET /metrics`

This endpoint returns the Prometheus text format and needs the API key if one is set. It exposes
these metric families:

- `scrape_jobs_total{status}`
- `resolver_results_total{field,method}`
- `candidates_discovered_total{country,source}`
- `crawl_requests_total{status_code}`
- `crawl_duration_seconds`
- `email_found_ratio{country,region,industry}`, labelled with canonical codes, not client labels
- `tiles_saturated_total{source}`
- `source_budget_used{source}`
- `smtp_results_total{result}`
- `websites_resolved_total{method}`: accepted companies by how their website was found:
  `osm_tag`, `email_domain`, `search`, `guess`, `web_discovery` (found by web search, not in OSM),
  `web_discovery_merged` (a web result merged into an OSM company without a website)
- `crawl_js_shells_total`: crawled homepages that look like JavaScript app shells (counted only;
  nothing is rendered)
