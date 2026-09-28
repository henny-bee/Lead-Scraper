# Company Lead Scraper API (v0.3, zero-config)

Send it a target (country, regions, industries, fields you want, max results). It returns
companies with their website and email. The country, region and industry values are free text in
any language and are interpreted when the request arrives.

The pipeline:
- **Resolve** the input.
- **Discover** companies in OpenStreetMap through the public Overpass API.
- **Crawl** each company's own website (homepage, legal notice, contact page).
- **Extract and score** emails and the other requested fields.
- **Verify** emails (optional).
- **Deliver** the result by polling or by callback.

**Zero-config:**
- It needs no API keys, no PostgreSQL, no Redis and no paid services.
- It runs as one container and every setting has a working default.
- Jobs live only in RAM plus a per-job temp directory.
- **Reading never deletes.** Results can be read and exported as often as you like. Remove a job
  with `DELETE`, or it expires by TTL (15 min after finishing, 30 min after failing). A `2xx`
  callback reply also deletes it.

**It is polite, so it is not instant.**
- Overpass calls are rate-limited to 1 at a time and 10 per minute per process.
- Crawling uses 1 connection per domain, waits 2 s between requests, fetches at most 5 pages per
  domain and respects robots.txt.
- A request with 3 regions × 4 industries (12 slices) needs about **70 s of discovery alone**.
  Use `?wait`, polling or a callback.
- The public Overpass instance is shared and sometimes overloaded. It can answer `429`/`504`,
  or time out on very large areas (a whole country with `regions: []`). When that happens the
  slice is skipped with a warning and the job still ends with `success`, possibly with fewer
  results. Prefer region-level requests. For heavy use, point `OVERPASS_URL` at a self-hosted
  instance.

## Quick start

```bash
cp .env.example .env          # optional: every variable has a working default
docker compose up -d --build
curl http://localhost:8000/health          # {"status":"ok",...} once the resolver index is warm
```

Dry run (see how your input is interpreted; no scraping):

```bash
curl -X POST http://localhost:8000/scrape/resolve -H 'Content-Type: application/json' -d '{
  "country": "XXXXX", "regions": ["XXXX", "XXXX"],
  "industries": ["Manufacturing", "Logistics"],
  "information": ["company_name", "company_email", "website"], "max_output": 50}'
```

Start a job. It returns `202 {"status":"queued","job_id":"scr_…","poll_url":"/scrape/scr_…"}`.
With `?wait=20`, if the job finishes within 20 s you get `200` and the final body instead:

```bash
curl -X POST 'http://localhost:8000/scrape?wait=20' -H 'Content-Type: application/json' -d '{
  "country": "XXXXX", "regions": ["XXXXX", "XXXXX"], "industries": ["Maschinenbau", "Logistik"],
  "information": ["company_name", "company_email", "website"], "max_output": 100}'

curl http://localhost:8000/scrape/scr_XXXXXXXX     # poll: running → success (final body; repeatable)
curl 'http://localhost:8000/scrape/scr_XXXXXXXX/export?format=csv'   # CSV, also repeatable
curl -X DELETE http://localhost:8000/scrape/scr_XXXXXXXX   # optional cleanup / cancel → 204
```

## Endpoints

| Method & path | Purpose |
|---|---|
| `GET /health` | Readiness (resolver warm, temp dir writable); never needs auth |
| `POST /scrape/resolve` | Dry run: resolved country/regions/industries, sources, warnings |
| `POST /scrape[?wait=0..20]` | Create a scrape job (idempotent while the job is in RAM) |
| `GET /scrape/{job_id}[?offset&limit]` | Progress while running, final result when done |
| `GET /scrape/{job_id}/export?format=csv` | Final result as CSV (xlsx not supported) |
| `DELETE /scrape/{job_id}` | Cancel if running and delete → `204` |
| `POST /verify` | Verify up to 50 emails synchronously; 51–10 000 → async job |
| `GET /verify/{job_id}` | Result of an async verify job |
| `GET /meta/countries`, `/meta/regions?country=`, `/meta/industries?q=` | Autocomplete data |
| `GET /metrics` | Prometheus metrics |

Interactive OpenAPI docs: `http://localhost:8000/docs`.

## Documentation

- [doc/setup.md](doc/setup.md): deployment, all environment variables, API key, SMTP
  verification, reverse proxy, local development, tests, troubleshooting.
- [doc/api.md](doc/api.md): every endpoint with real request/response examples, error format,
  status codes.
