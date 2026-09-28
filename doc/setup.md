# Setup and operations

## 1. Requirements

- **Docker deployment:** Docker Engine with Compose v2.24 or newer (`env_file` uses
  `required: false`).
- **Local development:** Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).
- **Outbound internet access** for the Overpass API, company websites and DNS. Outbound port 25
  is needed only if you enable SMTP verification.
- **Memory:** all job data lives in RAM and in the `tmpfs`, so size memory for your largest
  expected job (`max_output` up to 5000).

## 2. Docker deployment

```bash
cp .env.example .env            # optional; without .env every default applies
docker compose up -d --build
curl http://localhost:8000/health
```

The compose file runs one service, `lead-scraper`:
- port `8000:8000`, `restart: unless-stopped`
- `tmpfs` at `/tmp/leadscraper`

The image (`python:3.12-slim`) contains `src/`, `config/` and `data/`. It runs a **single
Uvicorn worker**. Do not add `--workers`, because jobs live in that one process's memory.

**Startup and health:**
- On startup the service builds the CLDR country index (about 34 000 names). This takes
  1.5–20 s depending on the CPU.
- Until the index is ready, `/health` returns `503 not_ready`.
- The image's `HEALTHCHECK` polls `/health` every 30 s, with a 60 s start period.

**Configuration files:**
- `config/` is baked into the image. After editing aliases, contact keywords, legal forms,
  scoring weights and similar files, rebuild the image (`docker compose up -d --build`).
- The exception is `config/suppression.txt`. It is re-read whenever its modification time
  changes, so edits take effect without a restart **if** the file is visible to the container.
  To edit it on a live container, add a bind mount such as
  `./config/suppression.txt:/app/config/suppression.txt:ro`.

## 3. Environment variables

All variables are optional. An empty value means "use the default". No other variables are read.

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `prod` | Environment label (logging) |
| `HOST` | `0.0.0.0` | Parsed but not used by the app: the Docker `CMD` hard-codes `--host 0.0.0.0` |
| `PORT` | `8000` | Parsed but not used by the app: the `CMD` hard-codes `--port 8000`. To expose another port, change the compose mapping (e.g. `"9000:8000"`). |
| `JOB_TTL_MINUTES` | `15` | Lifetime of a finished job (read or not); also the tombstone lifetime |
| `JOB_FAILED_TTL_MINUTES` | `30` | Lifetime of a failed job, so the error can still be read |
| `TEMP_DIR` | `/tmp/leadscraper` | Per-job intermediate files (a `tmpfs` in Docker) |
| `NOMINATIM_URL` | *(empty)* | Optional Nominatim base URL. It enables city/district regions (`München`, `Landkreis Rosenheim`). Empty means ISO 3166-2 regions and aliases only. |
| `NOMINATIM_MAX_RPS` | `1` | Request rate to Nominatim (the public instance allows at most 1/s) |
| `INDUSTRY_EMBEDDINGS_ENABLED` | `false` | Reserved; not implemented in v0.3 |
| `INDUSTRY_LLM_ENABLED` | `false` | Reserved; not implemented in v0.3 |
| `OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | Overpass endpoint (public or self-hosted) |
| `GOOGLE_PLACES_API_KEY` / `GOOGLE_PLACES_ENABLED` | *(empty)* / `false` | Reserved for a later optional adapter; no effect in v0.3 |
| `COMPANIES_HOUSE_API_KEY` / `COMPANIES_HOUSE_ENABLED` | *(empty)* / `false` | Reserved for a later optional adapter; no effect in v0.3 |
| `CRAWLER_USER_AGENT` | `LeadScraperBot/0.3 (+https://your-domain.de/bot)` | Used for crawling, Overpass and callbacks. **Set it to a URL describing your bot.** |
| `CRAWLER_GLOBAL_CONCURRENCY` | `16` | Concurrent website fetches per job |
| `CRAWLER_PER_DOMAIN_DELAY_S` | `2` | Pause between requests to the same domain |
| `CRAWLER_MAX_PAGES_PER_DOMAIN` | `5` | Pages fetched per company website (robots.txt excluded) |
| `CRAWLER_MAX_RESPONSE_MB` | `2` | Larger responses are aborted |
| `SMTP_VERIFY_ENABLED` | `false` | Enables SMTP probing in `POST /verify` (see §5) |
| `SMTP_HELO_HOST` | *(empty)* | `EHLO` hostname for SMTP probes |
| `SMTP_MAIL_FROM` | *(empty)* | `MAIL FROM` address for SMTP probes |
| `API_KEY` | *(empty)* | Empty means no authentication (see §4) |

Some limits are code constants in `src/leadscraper/constants.py`, not env vars. They include the
Overpass pacing, the per-IP rate limit, the 120-minute maximum job runtime, the 10 s callback
timeout, and the `/verify` limits of 50 and 10 000. Changing them means changing code.

## 4. Enabling the API key

Set `API_KEY=<long random string>` and restart. Every endpoint except `/health` then requires one
of these headers:

```
X-API-Key: <key>
Authorization: Bearer <key>
```

A missing key returns `401 unauthorized`; a wrong key returns `403 forbidden`. `/metrics` is
protected too, so configure the header in your Prometheus scrape config.

## 5. Enabling SMTP verification (optional)

Set all three variables:

```dotenv
SMTP_VERIFY_ENABLED=true
SMTP_HELO_HOST=verifier.your-domain.de
SMTP_MAIL_FROM=bounce@your-domain.de
```

SMTP is used **only** by `POST /verify` with `"smtp_check": true`. Scrape jobs never use it. The
probe runs `EHLO → MAIL FROM → RCPT TO`, then sends a random `RCPT TO` to detect catch-all
domains, then `QUIT`. It never sends `DATA`.

If any of the three variables is missing, `smtp_check` is ignored: results are DNS-level
`unknown` with reason `smtp_disabled`, and the response includes a warning.

Caveats:
- **Outbound port 25** must be open. Many cloud providers block it by default.
- Use a dedicated IP whose rDNS/PTR matches `SMTP_HELO_HOST`.
- Use a `MAIL FROM` domain with SPF that really accepts bounces.
- Never use the IP of your sending mail server.
- High probe volume can get the IP blocklisted. Watch `smtp_results_total{result}` for rising
  `unknown` or `blocked` counts.
- Greylisting (4xx):
  - Synchronous requests (≤ 50 emails) make one attempt and return `unknown`/`greylisted`.
  - Async verify jobs retry after 5, 15 and 60 minutes, bounded by the 120-minute job runtime
    limit.

## 6. Running behind a reverse proxy

For internet exposure, put a reverse proxy (TLS, auth) in front and set `API_KEY`.

The built-in rate limit (120 requests/min, burst 30 per client IP) uses the **socket peer
address**. `X-Forwarded-For` is deliberately not trusted. Behind a proxy, all clients therefore
share the proxy's single bucket, so configure per-client rate limiting in the proxy itself.

Proxy timeouts: `POST /scrape?wait=20` can hold a request for up to 20 s. A CSV export of a large
job is a single response.

## 7. Local development

```bash
uv sync                                   # runtime + dev dependencies (pytest, respx, aiosmtpd)
TEMP_DIR=./.tmp uv run uvicorn leadscraper.main:app --reload --port 8000
```

`TEMP_DIR` must be writable. On Windows, set it to a local folder. OpenAPI docs are at
`http://localhost:8000/docs`.

Dependency policy: the runtime dependencies in `pyproject.toml` are a closed list (see
`PLAN.md` §2.1). A test guards against forbidden imports (redis, SQLAlchemy, playwright, …).

## 8. Tests

```bash
uv run pytest -q                 # full offline suite (Overpass/websites/DNS mocked; SMTP via local aiosmtpd)
uv run pytest -m live -q -rA     # opt-in: ONE small real query against the public Overpass instance
```

The `live` test is deselected by default. It passes even when Overpass is busy and it only
emits a warning, so check its output for actual candidates. Run it once, not in a loop, because
the public instance has usage limits.

## 9. Updating the vendored email domain lists

`src/leadscraper/verification/data/disposable_domains.txt` (CC0) and `free_email_domains.txt`
(MIT) are snapshots. Each file has a header naming its source URL, commit and license. There is
deliberately no update script, and runtime stays offline. To refresh by hand:

1. Download the current list from the source repository named in the header:
   - `disposable-email-domains/disposable-email-domains`: `disposable_email_blocklist.conf`
   - `LukeRenton/free-email-domain-list`: the domain set in `free_email_domains/__init__.py`
2. Write one lower-case domain per line.
3. Keep the comment header and update the commit SHA and date. For the MIT list, keep the
   license text.
4. Check that every non-comment line is a plain domain, then run `uv run pytest -q` and rebuild
   the image.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/health` returns `503 not_ready` right after start | The resolver index is still warming (up to ~20 s on slow CPUs). If it persists, check `details.checks.temp_dir_writable` and make `TEMP_DIR` writable. |
| A job stays `running` for minutes | Expected: Overpass pacing is about 6 s per slice, and each domain gets a 2 s delay and up to 5 pages. Reduce regions × industries or `max_output`. |
| `count` is lower than `max_output`, status `success` | Sources exhausted. OSM coverage and email yield vary by country (tier C is lower). Check `progress` while running and the resolve `warnings`. |
| Few or no candidates, warnings about Overpass | The public Overpass instance was busy or rate-limiting (429/504) and the slice was marked exhausted. Retry later, or point `OVERPASS_URL` at a self-hosted instance. |
| Whole-country request (`regions: []`) returns few or no results | A country-wide Overpass query can exceed the public instance's time or memory limits (each query allows up to 180 s on the server, 240 s on the client). Request specific regions instead, or use a self-hosted Overpass. |
| `422 unresolved_region` for a city | City/district level needs `NOMINATIM_URL`. Otherwise use a province/state or add an alias in `config/overrides/aliases.yaml`. |
| `404` on a job you fetched earlier | Reading never deletes, so the job was removed by `DELETE`, by a `2xx` callback, by TTL (15 min after finishing, 30 min after failing), or by a container restart. Export within the TTL, or raise `JOB_TTL_MINUTES` in `.env`. |
| Callback never arrives | It is a single attempt with a 10 s timeout. Check reachability from the container and the logs (`callback_failed` / `callback_rejected`). The job stays pollable until its TTL. |
| `429 rate_limited` from behind a proxy | All clients share the proxy's IP bucket; see §6. |
| All jobs gone after a restart | By design: state is in memory only. |
