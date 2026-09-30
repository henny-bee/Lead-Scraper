"""Live benchmark."""

import json
import os
import re
import time
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from leadscraper.crawler.website import registered_domain
from leadscraper.extractors.text import page_text
from leadscraper.main import create_app
from leadscraper.observability import metrics
from leadscraper.settings import load_settings

pytestmark = pytest.mark.live

INFO = ["company_name", "company_email", "website"]
REQUESTS = {
    "L1": {"country": "Germany", "regions": ["Bremen"], "industries": ["Logistik"],
           "information": INFO, "max_output": 20},
    "L2": {"country": "Germany", "regions": ["Bremen", "Hamburg"],
           "industries": ["Logistik", "Maschinenbau"], "information": INFO, "max_output": 50},
}
POLL_S = 0.25
MAX_WAIT_S = 20 * 60
METHODS = ("osm_tag", "email_domain", "search", "guess", "web_discovery", "web_discovery_merged")
SOURCES = ("osm", "web_search")


def sample_sums(name: str, label: str, keys: tuple[str, ...]) -> dict[str, float]:
    out = {k: 0.0 for k in keys}
    for family in metrics.REGISTRY.collect():
        for s in family.samples:
            if s.name == name and s.labels.get(label) in out:
                out[s.labels[label]] += s.value
    return out


def spot_check(companies: list[dict], candidates: list[dict], crawl_dir: Path) -> list[dict]:
    """Evidence for the hand spot check: each company's source and the saved-page lines that contain
    its email or a postcode-like address line (no network)."""
    by_domain: dict[str, list[dict]] = {}
    for cand in candidates:
        if cand.get("website"):
            by_domain.setdefault(registered_domain(cand["website"]) or "", []).append(cand)
    texts = [page_text(p.read_text(encoding="utf-8", errors="replace"))
             for p in sorted(crawl_dir.glob("*.html"))] if crawl_dir.exists() else []
    out = []
    for c in companies:
        domain = registered_domain(c.get("website") or "") or ""
        cands = by_domain.get(domain, [])
        source = cands[0]["source"] if cands else "osm (website looked up)"
        email = (c.get("company_email") or "").lower()
        lines = [line for t in texts if email and email in t.lower() for line in t.splitlines()
                 if email in line.lower() or re.search(r"\b\d{5}\b", line)]
        out.append({**c, "source": source,
                    "osm_postcode": next((x.get("postal_code") for x in cands if x.get("postal_code")), None),
                    "evidence": list(dict.fromkeys(lines))[:6]})
    return out


def crawl_status_counts() -> Counter:
    counts: Counter = Counter()
    for family in metrics.REGISTRY.collect():
        if family.name == "crawl_requests":
            for s in family.samples:
                if s.name == "crawl_requests_total":
                    counts[s.labels["status_code"]] += s.value
    return counts


def status_share(delta: Counter) -> dict[str, float]:
    buckets: Counter = Counter()
    for code, n in delta.items():
        if code == "error":
            key = "error"
        elif code in ("403", "429"):
            key = code
        elif code[:1] in "2345" and code.isdigit():
            key = f"{code[0]}xx" if code[0] != "4" else "4xx_other"
        else:
            key = "other"
        buckets[key] += n
    total = sum(buckets.values())
    return {k: round(v / total, 4) for k, v in sorted(buckets.items())} if total else {}


@pytest.mark.parametrize("name", ["L1", "L2"])
def test_live_benchmark(name: str, tmp_path: Path) -> None:
    body = REQUESTS[name]
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs")}))
    before = crawl_status_counts()
    resolved_before = sample_sums("websites_resolved_total", "method", METHODS)
    found_before = sample_sums("candidates_discovered_total", "source", SOURCES)
    marks: dict[str, float | None] = {"time_to_10_s": None, "time_to_target_s": None}
    with TestClient(app) as client:
        t0 = time.perf_counter()
        r = client.post("/scrape", json=body)
        assert r.status_code == 202, r.text
        job = app.state.jobs.get(r.json()["job_id"])
        while not job.status.is_terminal and time.perf_counter() - t0 < MAX_WAIT_S:
            now = time.perf_counter() - t0
            if marks["time_to_10_s"] is None and job.count >= 10:
                marks["time_to_10_s"] = round(now, 2)
            if marks["time_to_target_s"] is None and job.count >= body["max_output"]:
                marks["time_to_target_s"] = round(now, 2)
            time.sleep(POLL_S)
        wall = time.perf_counter() - t0
        status = job.status.value if job.status.is_terminal else "timeout"
        final = client.get(f"/scrape/{job.job_id}").json()
        companies = final.get("companies") or []
        progress = dict(job.progress)
        warnings = list((job.resolved or {}).get("warnings", []))
        tagged: set[str] = set()
        candidates: list[dict] = []
        if job.candidates_path.exists():
            for line in job.candidates_path.read_text(encoding="utf-8").splitlines():
                cand = json.loads(line)
                candidates.append(cand)
                if cand.get("website") and cand.get("source") == "osm":
                    tagged.add(cand["source_ref"])
        gate = app.state.search_gate
        search = {"backend": app.state.settings.web_search_backend, "requests": gate.requests,
                  "breaker_tripped": gate.paused_until > time.monotonic(),
                  "blocked_warnings": [w for w in warnings if "blocked" in w or "robots" in w]}
        if os.environ.get("LIVE_SPOTCHECK_DIR"):
            target = Path(os.environ["LIVE_SPOTCHECK_DIR"]) / f"spotcheck_{name}.json"
            target.write_text(json.dumps(spot_check(companies, candidates, job.crawl_dir),
                                         ensure_ascii=False, indent=1), encoding="utf-8")
        if status == "timeout":
            client.delete(f"/scrape/{job.job_id}")
    for key, n in (("time_to_10_s", 10), ("time_to_target_s", body["max_output"])):
        if marks[key] is None and len(companies) >= n:
            marks[key] = round(wall, 2)
    count = len(companies)
    with_email = sum(1 for c in companies if c.get("company_email"))
    with_site = sum(1 for c in companies if c.get("website"))
    same = sum(1 for c in companies if c.get("company_email") and c.get("website")
               and registered_domain(c["website"]) == registered_domain(c["company_email"].split("@")[1]))
    crawled = progress.get("crawled", 0)
    pct = (lambda n: round(100 * n / count, 1) if count else None)
    delta = crawl_status_counts() - before
    resolved_after = sample_sums("websites_resolved_total", "method", METHODS)
    found_after = sample_sums("candidates_discovered_total", "source", SOURCES)
    by_source = {m: int(resolved_after[m] - resolved_before[m]) for m in METHODS
                 if resolved_after[m] - resolved_before[m]}
    out = {"scenario": name, "wall_s": round(wall, 2), **marks, "status": status, "count": count,
           "target": body["max_output"], "fill_rate": round(count / body["max_output"], 4),
           "with_email_pct": pct(with_email), "with_website_pct": pct(with_site),
           "same_domain_email_pct": pct(same),
           "email_yield": round(progress.get("with_email", 0) / crawled, 4) if crawled else None,
           "candidates": progress.get("candidates", 0), "candidates_with_website_tag": len(tagged),
           "crawled": crawled, "warnings": warnings, "crawl_requests": int(sum(delta.values())),
           "crawl_status_share": status_share(delta), "by_source": by_source,
           "by_source_share": {m: round(n / count, 4) for m, n in by_source.items()} if count else {},
           "candidates_by_source": {s: int(found_after[s] - found_before[s]) for s in SOURCES},
           "web_search": search}
    print("\nBENCH-LIVE " + json.dumps(out, ensure_ascii=False))
    assert status in ("success", "timeout")
