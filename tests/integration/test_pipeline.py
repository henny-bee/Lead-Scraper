"""End-to-end scrape pipeline with mocked Overpass + mocked company websites (respx) and fake DNS.
No network: HTTP is fully mocked (assert_all_mocked), DNS lookups use fakes."""

import asyncio
import itertools
import json
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs

import dns.name
import dns.resolver
import httpx
import pytest
import respx

from leadscraper.domain.models import CompanyCandidate, JobStatus
from leadscraper.jobs.manager import JobManager
from leadscraper.observability import metrics
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.services.scrape_service import PipelineDeps, runner_for
from leadscraper.services.verify_service import EmailVerifier
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import OverpassGate
from leadscraper.verification import lists
from leadscraper.verification.dns import DnsChecker

pytestmark = pytest.mark.anyio

OVERPASS = "https://overpass.test/api/interpreter"
HTML = {"content-type": "text/html; charset=utf-8"}
BODY = {"country": "Germany", "regions": ["NRW"], "industries": ["Maschinenbau"],
        "information": ["company_name", "company_email", "website"], "max_output": 10}


def node(i: int, name: str, website: str | None = None, **tags: str) -> dict:
    t = {"name": name, **tags}
    if website:
        t["website"] = website
    return {"type": "node", "id": i, "lat": 51.2, "lon": 6.8, "tags": t}


ELEMENTS = [
    node(1, "Muster Maschinenbau", "https://muster-maschinenbau-example.de"),
    node(2, "Demo Anlagenbau", "demo-anlagen-example.de"),                       # no email on site
    node(3, "Probe Maschinen", "https://probe-maschinen-example.de"),            # marketing objection
    node(4, "Beispiel Maschinenbau GmbH", "https://www.beispiel-mb-example.de"),
    node(5, "Facebook Maschinen", "https://www.facebook.com/fbmaschinen"),  # social only → skipped
    node(6, "Ohne Website Maschinenbau"),                                   # no website → skipped
    node(1, "Muster Maschinenbau", "https://muster-maschinenbau-example.de"),         # duplicate element
]

SITES = {
    "https://muster-maschinenbau-example.de/": ('<html lang="de"><a href="/impressum">Impressum</a></html>'),
    "https://muster-maschinenbau-example.de/impressum": (
        "<html><body><p>Muster Maschinenbau GmbH</p><p>Industriestr. 1</p><p>40210 Düsseldorf</p>"
        "<p>E-Mail: info@muster-maschinenbau-example.de</p><p>Datenschutz: datenschutz@muster-maschinenbau-example.de</p>"
        "<p>Webdesign: hallo@agentur-web-example.de</p></body></html>"),
    "https://demo-anlagen-example.de/": '<html lang="de"><a href="/kontakt">Kontakt</a></html>',
    "https://demo-anlagen-example.de/kontakt": "<html><body>Rufen Sie uns an: 0211 123456</body></html>",
    "https://probe-maschinen-example.de/": '<html><a href="/impressum">Impressum</a></html>',
    "https://probe-maschinen-example.de/impressum": (
        "<html><body>Probe Maschinen KG, info@probe-maschinen-example.de. Der Nutzung der Kontaktdaten "
        "zur Übersendung von nicht ausdrücklich angeforderter Werbung wird widersprochen.</body></html>"),
    "https://www.beispiel-mb-example.de/": ('<html lang="de"><a href="/kontakt">Kontakt</a>'
                                  '<a href="/impressum">Impressum</a></html>'),
    "https://www.beispiel-mb-example.de/impressum": ("<html><body>Beispiel Maschinenbau GmbH<br>"
                                           "kontakt [at] beispiel-mb-example [punkt] de</body></html>"),
    "https://www.beispiel-mb-example.de/kontakt": ("<html><body>Vertrieb: vertrieb@beispiel-vertrieb-example.de"
                                         "</body></html>"),
}


async def public_dns(_host: str) -> list[str]:
    return ["93.184.216.34"]


class MX:
    def __init__(self, host: str) -> None:
        self.preference, self.exchange = 10, dns.name.from_text(host)


class FakeMailDns:
    def __init__(self, dead: set[str]) -> None:
        self.dead = dead

    async def resolve(self, qname, rdtype, **_kw):
        if qname in self.dead:
            raise dns.resolver.NXDOMAIN()
        return [MX(f"mx.{qname}.")]


def make_deps(tmp_path: Path, dead_mail_domains: set[str] = frozenset(), **env: str) -> tuple:
    settings = load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "OVERPASS_URL": OVERPASS, "WEB_SEARCH_URL": "off",
                              "CRAWLER_PER_DOMAIN_DELAY_S": "0", **env})
    supp = tmp_path / "suppression.txt"
    supp.write_text("", encoding="utf-8")
    deps = PipelineDeps(
        settings=settings, resolver=Resolver(settings), gate=OverpassGate(per_minute=600_000),
        verifier_factory=lambda s: EmailVerifier(s, dns=DnsChecker(FakeMailDns(set(dead_mail_domains))),
                                                 suppression=lists.SuppressionList(path=supp)),
        dns_resolve=public_dns, suppression=lists.SuppressionList(path=supp))
    return deps, JobManager(settings.temp_dir)


def mock_web(mock: respx.MockRouter, elements=ELEMENTS, slow: float = 0.0):
    overpass = mock.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": elements}))
    mock.get(url__regex=r"https?://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))

    async def site(request: httpx.Request) -> httpx.Response:
        if slow:
            await asyncio.sleep(slow)
        url = str(request.url)
        if url in SITES:
            return httpx.Response(200, headers=HTML, text=SITES[url])
        return httpx.Response(404, headers=HTML, text="<html>404</html>")

    mock.get(url__regex=r"https?://.*").mock(side_effect=site)
    return overpass


async def run_job(deps, jobs: JobManager, body: dict):
    job, _ = jobs.create("scrape", body, target=body["max_output"])
    await jobs.start(job, runner_for(deps, jobs))
    return job


async def test_end_to_end_only_requested_fields_and_labels(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    before = metrics.REGISTRY.get_sample_value("candidates_discovered_total",
                                               {"country": "DE", "source": "osm"}) or 0.0
    with respx.mock(assert_all_mocked=True) as mock:
        overpass = mock_web(mock)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS, job.error
    companies = job.result
    assert [c["website"] for c in companies] == ["https://muster-maschinenbau-example.de",
                                                 "https://www.beispiel-mb-example.de"] or \
        sorted(c["website"] for c in companies) == ["https://muster-maschinenbau-example.de",
                                                    "https://www.beispiel-mb-example.de"]
    by_site = {c["website"]: c for c in companies}
    muster = by_site["https://muster-maschinenbau-example.de"]
    assert muster == {"company_name": "Muster Maschinenbau GmbH",
                     "company_email": "info@muster-maschinenbau-example.de",
                     "website": "https://muster-maschinenbau-example.de",
                     "country": "Germany", "region": "NRW", "industry": "Maschinenbau"}
    beispiel = by_site["https://www.beispiel-mb-example.de"]
    assert beispiel["company_email"] == "kontakt@beispiel-mb-example.de"
    assert list(beispiel) == ["company_name", "company_email", "website", "country", "region", "industry"]
    assert job.count == 2
    assert job.progress["candidates"] == 6 and job.progress["with_email"] == 2
    assert job.progress["crawled"] == 4                   # facebook-only + no-website skipped
    # query shape + metrics
    query = parse_qs(overpass.calls[0].request.content.decode())["data"][0]
    assert 'area["ISO3166-2"="DE-NW"]' in query
    after = metrics.REGISTRY.get_sample_value("candidates_discovered_total", {"country": "DE", "source": "osm"})
    assert after == before + 6
    ratio = metrics.REGISTRY.get_sample_value(
        "email_found_ratio", {"country": "DE", "region": "iso:DE-NW", "industry": "machinery"})
    assert ratio == pytest.approx(2 / 4)
    # temp files
    lines = job.candidates_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6 and json.loads(lines[0])["source_ref"] == "node/1"   # dup element dropped
    assert len(list(job.crawl_dir.glob("*.html"))) >= 6
    result = json.loads(job.result_path.read_text(encoding="utf-8"))
    assert result["count"] == 2 and result["companies"] == companies
    assert job.resolved["regions"][0]["input"] == "NRW" and job.resolved["known_in_job"] == 2


async def test_company_without_email_not_counted_and_early_stop(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, {**BODY, "max_output": 1})
    assert job.status is JobStatus.SUCCESS and job.count == 1 and len(job.result) == 1
    assert job.result[0]["company_email"]


async def test_without_company_email_field_all_crawled_sites_count(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    body = {**BODY, "information": ["website"], "exclude_marketing_objections": False}
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, body)
    sites = sorted(c["website"] for c in job.result)
    assert sites == ["https://demo-anlagen-example.de", "https://muster-maschinenbau-example.de",
                     "https://probe-maschinen-example.de", "https://www.beispiel-mb-example.de"]
    assert all(set(c) == {"website", "country", "region", "industry"} for c in job.result)


async def test_objection_kept_when_not_excluded(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, {**BODY, "exclude_marketing_objections": False})
    assert "info@probe-maschinen-example.de" in {c["company_email"] for c in job.result}


async def test_verify_emails_falls_back_to_next_best_or_drops(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path, dead_mail_domains={"beispiel-mb-example.de", "muster-maschinenbau-example.de",
                                                         "agentur-web-example.de"})
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, {**BODY, "verify_emails": True})
    assert job.status is JobStatus.SUCCESS
    emails = {c["company_email"] for c in job.result}
    assert emails == {"vertrieb@beispiel-vertrieb-example.de"}     # Beispiel: next-best; Muster: dropped
    for c in job.result:                                  # no verification fields in the output
        assert set(c) == {"company_name", "company_email", "website", "country", "region", "industry"}


async def test_suppressed_email_never_used(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    deps.suppression.path.write_text("info@muster-maschinenbau-example.de\n", encoding="utf-8")
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, BODY)
    muster = next(c for c in job.result if c["website"] == "https://muster-maschinenbau-example.de")
    assert muster["company_email"] != "info@muster-maschinenbau-example.de"


async def test_empty_regions_region_is_null(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        overpass = mock_web(mock)
        job = await run_job(deps, jobs, {**BODY, "regions": []})
    assert job.result and all(c["region"] is None and "region" in c for c in job.result)
    query = parse_qs(overpass.calls[0].request.content.decode())["data"][0]
    assert 'area["ISO3166-2"="DE-' in query                  # subdivision slices


async def test_without_industries_all_companies_are_searched(tmp_path: Path) -> None:
    """``industries`` is optional: without it the job queries OSM for any company (office/craft/
    industrial/works keys, no name regex) and still returns results."""
    deps, jobs = make_deps(tmp_path)
    body = {k: v for k, v in BODY.items() if k != "industries"}
    with respx.mock(assert_all_mocked=True) as mock:
        overpass = mock_web(mock)
        job = await run_job(deps, jobs, body)
    assert job.status == "success" and job.result
    query = parse_qs(overpass.calls[0].request.content.decode())["data"][0]
    assert '"name"~' not in query and 'nwr.named["office"];' in query


async def test_unresolvable_region_fails_job_with_envelope(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True):
        job = await run_job(deps, jobs, {**BODY, "regions": ["Bayerm"]})
    assert job.status is JobStatus.FAILED
    assert job.error["code"] == "unresolved_region"
    assert job.error["details"]["suggestions"] == [{"id": "iso:DE-BY", "name": "Bayern"}]
    assert not job.candidates_path.exists()


async def test_overpass_down_is_success_with_zero_results(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(OVERPASS).mock(return_value=httpx.Response(400))
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.result == [] and job.count == 0
    assert any("Overpass" in w for w in job.resolved["warnings"])


async def test_cancellation_mid_run_leaves_no_temp_files(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_web(mock, slow=0.5)
        job, _ = jobs.create("scrape", BODY, target=10)
        task = jobs.start(job, runner_for(deps, jobs))
        for _ in range(200):
            if job.candidates_path.exists() and job.progress.get("candidates", 0) > 0:
                break
            await asyncio.sleep(0.01)
        assert job.candidates_path.exists()
        await jobs.delete(job.job_id)
    assert task.done() and not job.temp_dir.exists()
    assert list(Path(jobs.temp_root).iterdir()) == []


async def test_max_runtime_cancel_purges_intermediate_files(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_web(mock, slow=0.5)
        job, _ = jobs.create("scrape", BODY, target=10)
        jobs.start(job, runner_for(deps, jobs))
        for _ in range(200):
            if job.progress.get("candidates", 0) > 0:
                break
            await asyncio.sleep(0.01)
        jobs.fail(job.job_id, "job_timeout", "too slow")       # what the sweeper does …
        await jobs.cancel_task(job)                              # … followed by cancellation
    assert job.status is JobStatus.FAILED and job.error["code"] == "job_timeout"
    assert not job.candidates_path.exists() and not job.crawl_dir.exists()
    assert not job.result_path.exists()


# --- website-first ordering, usable-only quota, top-up ----------------------------------------------
_NODE_IDS = itertools.count(1000)


def company_sites(prefix: str, n: int, *, with_email: bool, start: int = 1) -> tuple[list, dict]:
    """``n`` website-tagged OSM nodes + their mocked pages (home → Impressum, email optional)."""
    elements, sites = [], {}
    for i in range(start, start + n):
        domain = f"{prefix}-{i:02d}-example.de"
        elements.append(node(next(_NODE_IDS), f"{prefix.title()} Firma {i:02d}", f"https://{domain}"))
        sites[f"https://{domain}/"] = '<html lang="de"><a href="/impressum">Impressum</a></html>'
        body = f"E-Mail: info@{domain}" if with_email else "Telefon 0211 12345"
        sites[f"https://{domain}/impressum"] = f"<html><body><p>{prefix} Firma {i:02d} GmbH</p><p>{body}</p></body></html>"
    return elements, sites


def mock_sites(mock: respx.MockRouter, elements, sites: dict[str, str]):
    """Like ``mock_web`` with custom elements/sites; ``elements`` may map an ISO 3166-2 code to the
    elements of that area's slice."""
    async def overpass(request: httpx.Request) -> httpx.Response:
        if isinstance(elements, dict):
            query = parse_qs(request.content.decode())["data"][0]
            els = next((v for code, v in elements.items() if f'"ISO3166-2"="{code}"' in query), [])
        else:
            els = elements
        return httpx.Response(200, json={"elements": els})

    route = mock.post(OVERPASS).mock(side_effect=overpass)
    mock.get(url__regex=r"https?://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))

    def site(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in sites:
            return httpx.Response(200, headers=HTML, text=sites[url])
        return httpx.Response(404, headers=HTML, text="<html>404</html>")

    mock.get(url__regex=r"https?://.*").mock(side_effect=site)
    return route


def home_requests(mock: respx.MockRouter) -> int:
    return sum(1 for call in mock.calls if call.request.url.path == "/" and call.request.method == "GET")


async def test_candidates_without_website_do_not_starve_quota(tmp_path: Path) -> None:
    """30 candidates without a website followed by 10 with one (email on the Impressum),
    ``max_output=5``: the old code processed only the first ceil(5 × 1.5) = 8 candidates, all
    without a website, and returned **0** companies."""
    no_site = [node(i, f"Ohne Website {i:02d}") for i in range(1, 31)]
    with_site, sites = company_sites("mit", 10, with_email=True)
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, no_site + with_site, sites)
        job = await run_job(deps, jobs, {**BODY, "max_output": 5})
    assert job.status is JobStatus.SUCCESS and job.count == 5
    assert job.progress["candidates"] == 40                   # every new candidate is still counted
    first = json.loads(job.candidates_path.read_text(encoding="utf-8").splitlines()[0])
    assert first["website"]                                  # website-first ordering (tier 0)


async def test_top_up_uses_surplus_until_target(tmp_path: Path) -> None:
    """20 website-tagged sites, the first 10 without an email, ``max_output=8``: the old code
    stopped after its 12-candidate quota with **2** companies; the top-up now reaches 8."""
    without, sites_a = company_sites("leer", 10, with_email=False)
    with_mail, sites_b = company_sites("voll", 10, with_email=True)
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, without + with_mail, {**sites_a, **sites_b})
        job = await run_job(deps, jobs, {**BODY, "max_output": 8})
    assert job.status is JobStatus.SUCCESS and job.count == 8
    assert all(c["company_email"].endswith("-example.de") for c in job.result)


async def test_top_up_stops_when_pool_exhausted(tmp_path: Path) -> None:
    elements, sites = company_sites("drei", 3, with_email=True)
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, elements, sites)
        job = await asyncio.wait_for(run_job(deps, jobs, {**BODY, "max_output": 10}), timeout=30)
    assert job.status is JobStatus.SUCCESS and job.count == 3 and len(job.result) == 3


async def test_top_up_round_robin_across_slices(tmp_path: Path) -> None:
    """Two slices; within the initial quotas no site has an email, and only slice A's surplus has
    email sites → the top-up (round-robin in plan order) fills the remainder from A, labelled A."""
    a_quota, sa1 = company_sites("anw", 3, with_email=False)
    a_extra, sa2 = company_sites("amail", 4, with_email=True)
    b_quota, sb1 = company_sites("bhe", 3, with_email=False)
    b_extra, sb2 = company_sites("bleer", 4, with_email=False)
    deps, jobs = make_deps(tmp_path, CRAWLER_GLOBAL_CONCURRENCY="1")
    body = {**BODY, "regions": ["NRW", "Hessen"], "max_output": 4}
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, {"DE-NW": a_quota + a_extra, "DE-HE": b_quota + b_extra},
                   {**sa1, **sa2, **sb1, **sb2})
        job = await run_job(deps, jobs, body)
    assert job.status is JobStatus.SUCCESS and job.count == 4
    assert {c["region"] for c in job.result} == {"NRW"}
    assert {c["industry"] for c in job.result} == {"Maschinenbau"}
    assert all("amail-" in c["website"] for c in job.result)


async def test_reallocation_scheduled_before_first_drain(tmp_path: Path) -> None:
    """slice A has 2 usable sites, slice B 20; ``max_output=8`` → target 12,
    quotas 6/6."""
    a_sites, sa = company_sites("spars", 2, with_email=False)
    b_sites, sb = company_sites("dicht", 20, with_email=False)
    sites = {**sa, **sb}
    starts: list[float] = []
    ends: list[float] = []

    async def overpass(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["data"][0]
        return httpx.Response(200, json={"elements": a_sites if '"DE-NW"' in query else b_sites})

    async def site(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/robots.txt" or url not in sites:
            return httpx.Response(404, headers=HTML, text="")
        is_home = request.url.path == "/"
        if is_home:
            starts.append(time.monotonic())
        await asyncio.sleep(0.3)
        if is_home:
            ends.append(time.monotonic())
        return httpx.Response(200, headers=HTML, text=sites[url])

    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(OVERPASS).mock(side_effect=overpass)
        mock.get(url__regex=r"https?://.*").mock(side_effect=site)
        job = await run_job(deps, jobs, {**BODY, "regions": ["NRW", "Hessen"], "max_output": 8})
    assert job.status is JobStatus.SUCCESS
    first_twelve = sorted(starts)[:12]
    assert len(first_twelve) == 12 and max(first_twelve) < min(ends)


async def test_top_up_respects_processed_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At most ``TOPUP_MAX_PROCESSED_FACTOR × max_output`` candidates are processed per job."""
    from leadscraper import constants as C
    monkeypatch.setattr(C, "TOPUP_MAX_PROCESSED_FACTOR", 2)
    elements, sites = company_sites("nomail", 20, with_email=False)
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, elements, sites)
        job = await run_job(deps, jobs, {**BODY, "max_output": 2})
        homes = home_requests(mock)
    assert job.status is JobStatus.SUCCESS and job.count == 0
    assert job.progress["crawled"] == 4 and homes == 4        # 2 × max_output, then stop


# --- company concurrency ≠ connection concurrency, per-site budget ----------------------------------
GAP_TOLERANCE_S = max(0.010, 2 * time.get_clock_info("monotonic").resolution)


class WebStats:
    """Observed from the respx side effect: HTTP requests in flight (overall / per domain) and
    per-domain request start/end times."""

    def __init__(self) -> None:
        self.http = self.max_http = 0
        self.per_domain: dict[str, int] = {}
        self.max_per_domain = 0
        self.times: dict[str, list[tuple[float, float]]] = {}

    def min_gap(self) -> float:
        gaps = [b[0] - a[1] for spans in self.times.values()
                for a, b in zip(sorted(spans), sorted(spans)[1:])]
        return min(gaps)


def mock_timed_sites(mock: respx.MockRouter, elements, sites: dict[str, str], stats: WebStats,
                     latency: float = 0.02, hang: set[str] = frozenset()):
    mock.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": elements}))

    async def site(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        stats.http += 1
        stats.max_http = max(stats.max_http, stats.http)
        stats.per_domain[host] = stats.per_domain.get(host, 0) + 1
        stats.max_per_domain = max(stats.max_per_domain, stats.per_domain[host])
        start = time.monotonic()
        try:
            await asyncio.sleep(30 if host in hang else latency)
            url = str(request.url)
            if request.url.path == "/robots.txt" or url not in sites:
                return httpx.Response(404, headers=HTML, text="")
            return httpx.Response(200, headers=HTML, text=sites[url])
        finally:
            stats.http -= 1
            stats.per_domain[host] -= 1
            stats.times.setdefault(host, []).append((start, time.monotonic()))

    mock.get(url__regex=r"https?://.*").mock(side_effect=site)


def count_companies_in_flight(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from leadscraper.services import scrape_service
    seen = {"now": 0, "max": 0}
    real = scrape_service.crawl_site

    async def counting(*args, **kwargs):
        seen["now"] += 1
        seen["max"] = max(seen["max"], seen["now"])
        try:
            return await real(*args, **kwargs)
        finally:
            seen["now"] -= 1

    monkeypatch.setattr(scrape_service, "crawl_site", counting)
    return seen


async def run_forty(tmp_path: Path, delay: str) -> tuple:
    tmp_path.mkdir(parents=True, exist_ok=True)
    elements, sites = company_sites("para", 40, with_email=True)
    stats = WebStats()
    deps, jobs = make_deps(tmp_path, CRAWLER_GLOBAL_CONCURRENCY="4", CRAWLER_PER_DOMAIN_DELAY_S=delay)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_timed_sites(mock, elements, sites, stats)
        t0 = time.monotonic()
        job = await run_job(deps, jobs, {**BODY, "max_output": 40})
        wall = time.monotonic() - t0
    assert job.status is JobStatus.SUCCESS and job.count == 40
    return stats, wall


async def test_companies_in_flight_exceed_connection_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    companies = count_companies_in_flight(monkeypatch)
    stats, _ = await run_forty(tmp_path, "0.3")
    assert 4 < companies["max"] <= 16                       # 4 × CRAWLER_GLOBAL_CONCURRENCY
    assert stats.max_http <= 4                              # connections in flight unchanged


async def test_same_domain_delay_unchanged_under_higher_concurrency(tmp_path: Path) -> None:
    stats, _ = await run_forty(tmp_path, "0.3")
    assert stats.max_per_domain == 1
    assert stats.min_gap() >= 0.3 - GAP_TOLERANCE_S


async def test_throughput_ratio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from leadscraper import constants as C
    from leadscraper.schemas.scrape import ScrapeRequest
    from leadscraper.services.resolver import geo
    geo.warm_up()                                  # lazy resolver indexes: not part of either wall
    await Resolver(load_settings({})).resolve(ScrapeRequest.model_validate(BODY))
    _, wall4 = await run_forty(tmp_path / "f4", "0.2")
    monkeypatch.setattr(C, "CRAWLER_COMPANY_CONCURRENCY_FACTOR", 1)
    _, wall1 = await run_forty(tmp_path / "f1", "0.2")
    assert wall4 <= 0.5 * wall1, (wall4, wall1)


async def test_hanging_site_abandoned_others_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from leadscraper import constants as C
    monkeypatch.setattr(C, "CRAWL_SITE_BUDGET_S", 1.0)
    elements, sites = company_sites("zeit", 6, with_email=True)
    stats = WebStats()
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_timed_sites(mock, elements, sites, stats, hang={"zeit-03-example.de"})
        t0 = time.monotonic()
        job = await run_job(deps, jobs, {**BODY, "max_output": 10})
        wall = time.monotonic() - t0
    assert job.status is JobStatus.SUCCESS and job.count == 5 and wall < 10
    assert "https://zeit-03-example.de" not in {c["website"] for c in job.result}


# --- early exit after a legal page with a same-domain email ------------------------------------------
EXIT_HOME = ('<html lang="de"><a href="/impressum">Impressum</a> <a href="/kontakt">Kontakt</a> '
             '<a href="/ueber-uns">Über uns</a></html>')


def exit_site(impressum_body: str) -> tuple[list, dict[str, str]]:
    base = "https://frueh-example.de"
    sites = {f"{base}/": EXIT_HOME,
             f"{base}/impressum": f"<html><body><p>Früh Maschinenbau GmbH</p>{impressum_body}</body></html>",
             f"{base}/kontakt": "<html><body>Kontakt: vertrieb@frueh-example.de, Tel. 0211 555</body></html>",
             f"{base}/ueber-uns": "<html><body>Seit 1970.</body></html>"}
    return [node(1, "Früh Maschinenbau", base)], sites


def fetched_paths(mock: respx.MockRouter) -> list[str]:
    return [c.request.url.path for c in mock.calls
            if c.request.method == "GET" and c.request.url.path != "/robots.txt"]


async def run_exit_job(tmp_path: Path, impressum_body: str, **body) -> tuple:
    elements, sites = exit_site(impressum_body)
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, elements, sites)
        job = await run_job(deps, jobs, {**BODY, **body})
        paths = fetched_paths(mock)
    assert job.status is JobStatus.SUCCESS
    return job, paths


async def test_crawl_stops_after_legal_page_with_same_domain_email(tmp_path: Path) -> None:
    job, paths = await run_exit_job(tmp_path, "<p>E-Mail: info@frueh-example.de</p>")
    assert paths == ["/", "/impressum"]
    assert job.result[0]["company_email"] == "info@frueh-example.de"


async def test_no_early_exit_when_phone_requested(tmp_path: Path) -> None:
    job, paths = await run_exit_job(tmp_path, "<p>E-Mail: info@frueh-example.de</p>",
                                    information=["company_name", "company_email", "website", "phone"])
    assert sorted(paths) == ["/", "/impressum", "/kontakt", "/ueber-uns"]


async def test_no_early_exit_when_verify_emails(tmp_path: Path) -> None:
    job, paths = await run_exit_job(tmp_path, "<p>E-Mail: info@frueh-example.de</p>", verify_emails=True)
    assert sorted(paths) == ["/", "/impressum", "/kontakt", "/ueber-uns"]


async def test_no_early_exit_when_only_foreign_email_on_legal_page(tmp_path: Path) -> None:
    job, paths = await run_exit_job(
        tmp_path, "<p>E-Mail: frueh.maschinen@gmx.de</p><p>Webdesign: hallo@agentur-web-example.de</p>")
    assert "/kontakt" in paths
    assert job.result[0]["company_email"] == "vertrieb@frueh-example.de"


async def test_marketing_objection_on_impressum_still_detected_with_early_exit(tmp_path: Path) -> None:
    job, paths = await run_exit_job(
        tmp_path, "<p>E-Mail: info@frueh-example.de</p><p>Der Nutzung der Kontaktdaten zur Übersendung "
                  "von nicht ausdrücklich angeforderter Werbung wird widersprochen.</p>")
    assert paths == ["/", "/impressum"]                         # early exit happened ...
    assert job.result == [] and job.count == 0                  # ... and the objection still counts


# --- parallel slice discovery -------------------------------------------------------------------------
async def discovery_wall(tmp_path: Path, concurrency: int) -> tuple[float, int]:
    """4 slices (2 regions × 2 industries), 0.5 s Overpass latency, no candidates: job start → last
    Overpass response and the maximum of Overpass requests in flight."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    deps, jobs = make_deps(tmp_path)
    deps.gate = OverpassGate(per_minute=600_000, concurrency=concurrency)
    state = {"now": 0, "max": 0, "last": 0.0}

    async def overpass(_request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        try:
            await asyncio.sleep(0.5)
            return httpx.Response(200, json={"elements": []})
        finally:
            state["now"] -= 1
            state["last"] = time.monotonic()

    body = {**BODY, "regions": ["NRW", "Hessen"], "industries": ["Maschinenbau", "Logistik"]}
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(OVERPASS).mock(side_effect=overpass)
        job, _ = jobs.create("scrape", body, target=body["max_output"])
        t0 = time.monotonic()
        await jobs.start(job, runner_for(deps, jobs))
    assert job.status is JobStatus.SUCCESS and route.call_count == 4
    return state["last"] - t0, state["max"]


async def test_parallel_slices_faster(tmp_path: Path) -> None:
    serial, serial_max = await discovery_wall(tmp_path / "serial", 1)
    parallel, parallel_max = await discovery_wall(tmp_path / "parallel", 2)
    assert serial_max == 1 and parallel_max == 2
    assert parallel <= 0.65 * serial, (parallel, serial)


async def test_stop_cancels_remaining_slices(tmp_path: Path) -> None:
    """Target met while discovery is still running → the running slice is cancelled and no further
    Overpass request is sent (none starts once the job has its companies)."""
    elements, sites = company_sites("stopp", 3, with_email=True)
    deps, jobs = make_deps(tmp_path)
    deps.gate = OverpassGate(per_minute=600_000, concurrency=1)          # slices one after another
    holder: dict = {}
    counts_at_request: list[int] = []

    async def overpass(request: httpx.Request) -> httpx.Response:
        counts_at_request.append(holder["job"].count)
        holder.setdefault("first_request", time.monotonic())
        first = len(counts_at_request) == 1
        await asyncio.sleep(0.05 if first else 1.0)
        return httpx.Response(200, json={"elements": elements if first else []})

    async def site(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)          # the company is accepted ≈ 0.35 s after the first response
        url = str(request.url)
        if url in sites:
            return httpx.Response(200, headers=HTML, text=sites[url])
        return httpx.Response(404, headers=HTML, text="")

    # target 2 → quota 1 for the two alphabetically first slice keys (``allocate``), i.e. the two
    # Bayern slices; they come first in plan order, so the first response is scheduled at once.
    body = {**BODY, "regions": ["Bayern", "Hessen", "NRW"], "industries": ["Logistik", "Maschinenbau"],
            "max_output": 1}
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(OVERPASS).mock(side_effect=overpass)
        mock.get(url__regex=r"https?://.*").mock(side_effect=site)
        job, _ = jobs.create("scrape", body, target=1)
        holder["job"] = job
        await jobs.start(job, runner_for(deps, jobs))
        after_first = time.monotonic() - holder["first_request"]
    assert job.status is JobStatus.SUCCESS and job.count == 1
    # 2 requests started (respx only records completed calls, the second one was cancelled):
    assert len(counts_at_request) == 2 and route.call_count == 1
    assert all(c < 1 for c in counts_at_request)             # none started after the target was met
    assert after_first < 0.8                  # the in-flight 1.0 s slice request was cancelled too


async def test_no_early_exit_when_only_special_function_email_on_legal_page(tmp_path: Path) -> None:
    """a DPO address on the Impressum is no reason to stop."""
    elements, sites = exit_site("<p>Datenschutz: datenschutz@frueh-example.de</p>")
    sites["https://frueh-example.de/kontakt"] = "<html><body>Schreiben Sie an info@frueh-example.de</body></html>"
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, elements, sites)
        job = await run_job(deps, jobs, BODY)
        paths = fetched_paths(mock)
    assert "/kontakt" in paths
    assert job.result[0]["company_email"] == "info@frueh-example.de"


async def test_no_early_exit_when_legal_email_suppressed(tmp_path: Path) -> None:
    elements, sites = exit_site("<p>E-Mail: info@frueh-example.de</p>")
    sites["https://frueh-example.de/kontakt"] = "<html><body>Kontakt: kontakt@frueh-example.de</body></html>"
    deps, jobs = make_deps(tmp_path)
    deps.suppression.path.write_text("info@frueh-example.de\n", encoding="utf-8")
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, elements, sites)
        job = await run_job(deps, jobs, BODY)
        paths = fetched_paths(mock)
    assert "/kontakt" in paths
    assert job.result[0]["company_email"] == "kontakt@frueh-example.de"


# --- country-wide requests → subdivision slices --------------------------------------------------------
async def test_empty_regions_split_keeps_result_set(tmp_path: Path) -> None:
    """16 DE slices, the mock returns the same 6 elements for each → dedup keeps the same result
    set."""
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        overpass = mock_web(mock)
        job = await run_job(deps, jobs, {**BODY, "regions": []})
    assert overpass.call_count == 16
    assert sorted(c["website"] for c in job.result) == ["https://muster-maschinenbau-example.de",
                                                        "https://www.beispiel-mb-example.de"]
    assert job.progress["candidates"] == 6


# --- the pipeline probes fallbacks per missing kind ---------------------------------------------------
async def test_pipeline_probes_contact_fallback_when_only_legal_link(tmp_path: Path) -> None:
    base = "https://unverlinkt-example.de"
    sites = {f"{base}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{base}/impressum": "<html><body>Unverlinkt Maschinenbau GmbH, 40210 Düsseldorf</body></html>",
             f"{base}/kontakt": "<html><body>E-Mail: info@unverlinkt-example.de</body></html>"}
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, [node(1, "Unverlinkt Maschinenbau", base)], sites)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS
    assert [c["company_email"] for c in job.result] == ["info@unverlinkt-example.de"]


# --- website lookup from the OSM email domain + identity check ----------------------------------------
def email_node(i: int, name: str, email: str, postcode: str = "40210", city: str = "Düsseldorf") -> dict:
    return node(i, name, **{"email": email, "addr:postcode": postcode, "addr:city": city})


def resolved_count(method: str) -> float:
    return metrics.REGISTRY.get_sample_value("websites_resolved_total", {"method": method}) or 0.0


async def test_email_tag_only_candidate_crawled_at_email_domain(tmp_path: Path) -> None:
    base = "https://nurmail-maschinen-example.de"
    sites = {f"{base}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{base}/impressum": ("<html><body><p>Nurmail Maschinenbau GmbH</p><p>Werkstr. 4</p>"
                                   "<p>40210 Düsseldorf</p><p>kontakt@nurmail-maschinen-example.de</p></body></html>")}
    before = resolved_count("email_domain")
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, [email_node(1, "Nurmail Maschinenbau", "info@nurmail-maschinen-example.de")], sites)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 1
    company = job.result[0]
    assert company["website"] == base
    assert company["company_email"] in {"kontakt@nurmail-maschinen-example.de", "info@nurmail-maschinen-example.de"}
    assert job.progress["crawled"] == 1
    assert resolved_count("email_domain") == before + 1


async def test_free_mail_hint_never_becomes_website(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        gmx = mock.get(url__regex=r"https?://(www\.)?gmx\.de/.*")         # before the catch-all
        route = mock_sites(mock, [email_node(1, "Freemail Maschinenbau", "freemail.maschinen@gmx.de")], {})
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 0 and route.call_count == 1
    assert gmx.call_count == 0 and job.progress["crawled"] == 0


async def test_identity_mismatch_rejected(tmp_path: Path) -> None:
    """The email domain's Impressum names another company in another town → rejected, and the failed
    lookup is not counted as crawled."""
    base = "https://fremd-maschinen-example.de"
    sites = {f"{base}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{base}/impressum": ("<html><body><p>Ganz Andere Werkzeuge GmbH</p><p>Isarweg 1</p>"
                                   "<p>80331 München</p><p>info@fremd-maschinen-example.de</p></body></html>")}
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, [email_node(1, "Rheinufer Maschinenbau", "info@fremd-maschinen-example.de")], sites)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 0


async def test_failed_lookup_not_counted_as_crawled(tmp_path: Path) -> None:
    base = "https://fremd2-maschinen-example.de"
    sites = {f"{base}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{base}/impressum": "<html><body><p>Anderswo AG</p><p>10115 Berlin</p></body></html>"}
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, [email_node(1, "Rheinufer Maschinenbau", "info@fremd2-maschinen-example.de")], sites)
        job = await run_job(deps, jobs, BODY)
    assert job.count == 0 and job.progress["crawled"] == 0 and job.progress["candidates"] == 1


async def test_osm_website_tag_path_unchanged(tmp_path: Path) -> None:
    """Website-tagged candidates get no identity check; the golden counters stay (crawled == 4)."""
    before = resolved_count("osm_tag")
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, BODY)
    assert job.count == 2 and job.progress["crawled"] == 4
    assert resolved_count("osm_tag") == before + 2


async def test_failed_lookup_does_not_block_domain_for_tagged_candidate(tmp_path: Path) -> None:
    """slice 1's email-only candidate A fails the identity check at
    ``firma-b-example.de``; slice 2 (answered later) has candidate B tagged with that website → B
    is still crawled and accepted."""
    base = "https://firma-b-example.de"
    sites = {f"{base}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{base}/impressum": ("<html><body><p>Firma B Maschinenbau GmbH</p><p>Werkstr. 9</p>"
                                   "<p>35390 Gießen</p><p>info@firma-b-example.de</p></body></html>")}
    a = email_node(1, "Anders Maschinenbau", "info@firma-b-example.de")
    b = node(2, "Firma B Maschinenbau", base, **{"addr:postcode": "35390", "addr:city": "Gießen"})

    async def overpass(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["data"][0]
        if '"DE-HE"' in query:
            await asyncio.sleep(0.5)                          # slice 2 answers after A's lookup
            return httpx.Response(200, json={"elements": [b]})
        return httpx.Response(200, json={"elements": [a]})

    def site(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(200, headers=HTML, text=sites[url]) if url in sites else \
            httpx.Response(404, headers=HTML, text="")

    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(OVERPASS).mock(side_effect=overpass)
        mock.get(url__regex=r"https?://.*").mock(side_effect=site)
        job = await run_job(deps, jobs, {**BODY, "regions": ["NRW", "Hessen"]})
    assert job.status is JobStatus.SUCCESS
    assert [(c["website"], c["region"]) for c in job.result] == [(base, "Hessen")]


# --- search-based website lookup -------------------------------------------------------------------
KRANICH = "https://kranich-maschinen-example.de"
KRANICH_SITES = {f"{KRANICH}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
                 f"{KRANICH}/impressum": ("<html><body><p>Kranich Maschinenbau GmbH</p><p>Werkstr. 1</p>"
                                          "<p>40210 Düsseldorf</p><p>info@kranich-maschinen-example.de</p>"
                                          "</body></html>")}


def lookup_node(i: int, name: str, postcode: str = "40210", city: str = "Düsseldorf") -> dict:
    return node(i, name, **{"addr:postcode": postcode, "addr:city": city})


def with_search(deps, search_mock, backend: str = "ddg"):
    from leadscraper.sources.web_search import DuckDuckGoHtml, SearxngJson
    deps.search_backend = DuckDuckGoHtml() if backend == "ddg" else SearxngJson("http://searxng.test")
    deps.search_gate = search_mock.gate
    return deps


@pytest.mark.parametrize("backend", ["ddg", "searxng"])
async def test_search_lookup_finds_verified_site(tmp_path: Path, search_mock, backend: str) -> None:
    search_mock.serp("kranich maschinenbau", [("https://www.gelbeseiten.de/kranich", "Kranich - Gelbe Seiten"),
                                              (f"{KRANICH}/", "Kranich Maschinenbau GmbH")])
    before = resolved_count("search")
    deps, jobs = make_deps(tmp_path)
    with_search(deps, search_mock, backend)
    with respx.mock(assert_all_mocked=True) as mock:
        search_mock.install(mock)
        mock_sites(mock, [lookup_node(1, "Kranich Maschinenbau")], KRANICH_SITES)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 1
    assert job.result[0]["website"] == KRANICH and job.result[0]["company_email"] == "info@kranich-maschinen-example.de"
    assert search_mock.queries == ['"Kranich Maschinenbau" Düsseldorf']
    assert resolved_count("search") == before + 1 and job.progress["crawled"] == 1


async def test_search_lookup_skipped_when_off(tmp_path: Path) -> None:
    """Search off (deps without backend): no search request at all — the conftest guard would fail
    the test otherwise — and the candidate is not processed."""
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_sites(mock, [lookup_node(1, "Kranich Maschinenbau")], KRANICH_SITES)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 0 and job.progress["crawled"] == 0


async def test_search_decoy_same_name_other_city_rejected(tmp_path: Path, search_mock) -> None:
    decoy = "https://kranich-maschinenbau-example.de"
    sites = {f"{decoy}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{decoy}/impressum": ("<html><body><p>Kranich Maschinenbau GmbH</p><p>Isarweg 1</p>"
                                    "<p>80331 München</p><p>info@kranich-maschinenbau-example.de</p></body></html>")}
    search_mock.serp("kranich maschinenbau", [(f"{decoy}/", "Kranich Maschinenbau München")])
    deps, jobs = make_deps(tmp_path)
    with_search(deps, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        search_mock.install(mock)
        mock_sites(mock, [lookup_node(1, "Kranich Maschinenbau")], sites)
        job = await run_job(deps, jobs, BODY)
    assert job.count == 0 and job.progress["crawled"] == 0 and search_mock.queries


async def test_no_search_when_tagged_candidates_meet_target(tmp_path: Path, search_mock) -> None:
    tagged, sites = company_sites("genug", 3, with_email=True)
    deps, jobs = make_deps(tmp_path)
    with_search(deps, search_mock)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        search_mock.install(mock)
        mock_sites(mock, tagged + [lookup_node(99, "Kranich Maschinenbau")], {**sites, **KRANICH_SITES})
        job = await run_job(deps, jobs, {**BODY, "max_output": 3})
    assert job.count == 3 and search_mock.queries == []


async def test_search_budget_exhausted_warns_once(tmp_path: Path, search_mock,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from leadscraper import constants as C
    monkeypatch.setattr(C, "WEB_SEARCH_BUDGET_PER_JOB", 2)      # robots.txt is not counted
    deps, jobs = make_deps(tmp_path)
    with_search(deps, search_mock)
    candidates = [lookup_node(i, f"Suchlos Maschinenbau {i:02d}", postcode=f"402{i:02d}") for i in range(1, 7)]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        search_mock.install(mock)
        mock_sites(mock, candidates, {})
        job = await run_job(deps, jobs, BODY)
    assert job.count == 0 and len(search_mock.queries) == 2
    budget_warnings = [w for w in job.resolved["warnings"] if "budget" in w.lower()]
    assert len(budget_warnings) == 1


async def test_target_met_does_not_wait_for_queued_lookups(tmp_path: Path, search_mock) -> None:
    """with the production search interval (3 s) and lookups queued on the gate when the
    target is met, the job ends right away (< 1 s after the target), not after the queue."""
    from leadscraper.sources.web_search import SearchGate
    search_mock.serp("kranich maschinenbau", [(f"{KRANICH}/", "Kranich Maschinenbau GmbH")])
    deps, jobs = make_deps(tmp_path)
    with_search(deps, search_mock)
    deps.search_gate = SearchGate()                              # production WEB_SEARCH_MIN_INTERVAL_S
    candidates = [lookup_node(1, "Kranich Maschinenbau")] + [
        lookup_node(i, f"Warte Maschinenbau {i:02d}", postcode=f"402{i:02d}") for i in range(2, 7)]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        search_mock.install(mock)
        mock_sites(mock, candidates, KRANICH_SITES)
        job, _ = jobs.create("scrape", {**BODY, "max_output": 1}, target=1)
        task = jobs.start(job, runner_for(deps, jobs))
        target_at = None
        while not task.done():
            await asyncio.wait({task}, timeout=0.02)
            if target_at is None and job.count >= 1:
                target_at = time.monotonic()
        ended = time.monotonic()
        await task
    assert job.status is JobStatus.SUCCESS and job.count == 1 and target_at is not None
    assert ended - target_at < 1.0
    assert search_mock.queries == ['"Kranich Maschinenbau" Düsseldorf']    # the queued ones never ran


# --- review: strict location for search results (the site's own address) ------------------------------
DECOY = "https://kranich-maschinenbau-example.de"


def decoy_sites(extra_line: str) -> dict[str, str]:
    return {f"{DECOY}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
            f"{DECOY}/impressum": ("<html><body><p>Kranich Maschinenbau GmbH</p><p>Leopoldstr. 5</p>"
                                   "<p>80331 München</p><p>info@kranich-maschinenbau-example.de</p>"
                                   f"<p>{extra_line}</p></body></html>")}


@pytest.mark.parametrize("extra_line", ["Registergericht: Amtsgericht Düsseldorf",
                                        "Niederlassungen: Hamburg, Düsseldorf, Köln"])
async def test_search_decoy_mentioning_candidate_city_rejected(tmp_path: Path, search_mock,
                                                               extra_line: str) -> None:
    """A same-name company elsewhere whose page mentions the candidate's city (court, branch list)
    is not accepted for a search result: its own address (80331 München) decides."""
    search_mock.serp("kranich maschinenbau", [(f"{DECOY}/", "Kranich Maschinenbau")])
    deps, jobs = make_deps(tmp_path)
    with_search(deps, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        search_mock.install(mock)
        mock_sites(mock, [lookup_node(1, "Kranich Maschinenbau")], decoy_sites(extra_line))
        job = await run_job(deps, jobs, BODY)
    assert search_mock.queries and job.count == 0 and job.progress["crawled"] == 0


async def test_email_domain_keeps_lenient_location(tmp_path: Path) -> None:
    """The same page reached through the OSM email domain keeps 's lenient rule (branch offices with
    a head-office Impressum): the candidate's city on the page is enough."""
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, [email_node(1, "Kranich Maschinenbau", "info@kranich-maschinenbau-example.de")],
                   decoy_sites("Niederlassungen: Hamburg, Düsseldorf, Köln"))
        job = await run_job(deps, jobs, BODY)
    assert job.count == 1 and job.result[0]["website"] == DECOY


# --- domain guessing --------------------------------------------------------------------------------
GUESSED = "https://kranich-maschinenbau.de"            # the first slug of "Kranich Maschinenbau"
GUESSED_HOSTS = {"kranich-maschinenbau.de", "kranichmaschinenbau.de", "kranich-maschinenbau.com"}
OWN_ADDRESS = "<p>Kranich Maschinenbau GmbH</p><p>Werkstr. 1</p><p>40210 Düsseldorf</p>"


def guessed_sites(body: str) -> dict[str, str]:
    return {f"{GUESSED}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
            f"{GUESSED}/impressum": f"<html><body>{body}<p>info@kranich-maschinenbau.de</p></body></html>"}


@pytest.mark.parametrize("element,body,accepted", [
    (lookup_node(1, "Kranich Maschinenbau"), OWN_ADDRESS, True),
    (lookup_node(1, "Kranich Maschinenbau"),                    # same name elsewhere, court in the city
     "<p>Kranich Maschinenbau GmbH</p><p>Leopoldstr. 5</p><p>80331 München</p>"
     "<p>Registergericht: Amtsgericht Düsseldorf</p>", False),
    (lookup_node(1, "Kranich Maschinenbau"), "<p>Kranich Maschinenbau GmbH</p>", False),  # no location
    (node(1, "Kranich Maschinenbau"), "<p>Kranich Maschinenbau GmbH</p>", False),  # no name-only rule
], ids=["own-address", "other-postcode", "no-location", "candidate-without-address"])
async def test_guess_requires_location_match(tmp_path: Path, element: dict, body: str,
                                             accepted: bool) -> None:
    before = resolved_count("guess")
    deps, jobs = make_deps(tmp_path)
    assert deps.website_guess                                  # on by default
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, [element], guessed_sites(body))
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS
    assert job.count == int(accepted) and job.progress["crawled"] == int(accepted)
    assert resolved_count("guess") == before + int(accepted)
    if accepted:
        assert job.result[0]["website"] == GUESSED
        assert job.result[0]["company_email"] == "info@kranich-maschinenbau.de"


async def test_name_only_page_accepted_by_email_domain_not_by_guess(tmp_path: Path) -> None:
    """The name-only page of the last case above is accepted when the same site comes from the OSM
    email domain, so the guess rejection is ``require_location``."""
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_sites(mock, [node(1, "Kranich Maschinenbau", email="info@kranich-maschinenbau.de")],
                   guessed_sites("<p>Kranich Maschinenbau GmbH</p>"))
        job = await run_job(deps, jobs, BODY)
    assert job.count == 1 and job.result[0]["website"] == GUESSED


@pytest.mark.parametrize("answer", ["nxdomain", "no-address", "private"])
async def test_nxdomain_makes_no_http_request(tmp_path: Path, answer: str) -> None:
    asked: list[str] = []

    async def resolve(host: str) -> list[str]:
        asked.append(host)
        if host not in GUESSED_HOSTS:
            return ["93.184.216.34"]
        if answer == "nxdomain":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [] if answer == "no-address" else ["10.0.0.7"]

    deps, jobs = make_deps(tmp_path)
    deps.dns_resolve = resolve
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_sites(mock, [lookup_node(1, "Kranich Maschinenbau")], guessed_sites(OWN_ADDRESS))
        job = await run_job(deps, jobs, BODY)
        requests = [c.request for c in mock.calls]            # respx resets its calls on exit
    assert job.status is JobStatus.SUCCESS and job.count == 0 and job.progress["crawled"] == 0
    assert GUESSED_HOSTS <= set(asked)                         # every guess was DNS-checked …
    assert [r.url for r in requests if r.method == "GET"] == []   # … none fetched (not even robots.txt)
    assert [r.method for r in requests] == ["POST"]            # only the Overpass query


async def test_guessing_on_keeps_golden_counters(tmp_path: Path) -> None:
    """Counting rule with guessing on: the facebook-only and no-website candidates get a guess (fake
    DNS: public, respx: 404) that fails the identity check → crawled == 4, ratio 2/4."""
    before = resolved_count("guess")
    deps, jobs = make_deps(tmp_path)
    assert deps.website_guess
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, BODY)
        hosts = {c.request.url.host for c in mock.calls}      # respx resets its calls on exit
    assert {"ohne-website-maschinenbau.de", "facebook-maschinen.de"} <= hosts
    assert job.count == 2 and job.progress["crawled"] == 4 and job.progress["with_email"] == 2
    ratio = metrics.REGISTRY.get_sample_value(
        "email_found_ratio", {"country": "DE", "region": "iso:DE-NW", "industry": "machinery"})
    assert ratio == pytest.approx(2 / 4) and resolved_count("guess") == before


async def test_guessing_off_requests_no_guessed_host(tmp_path: Path) -> None:
    deps, jobs = make_deps(tmp_path)
    deps.website_guess = False
    with respx.mock(assert_all_mocked=True) as mock:
        mock_web(mock)
        job = await run_job(deps, jobs, BODY)
        hosts = {c.request.url.host for c in mock.calls}
    assert "muster-maschinenbau-example.de" in hosts          # the calls were recorded
    assert not hosts & {"ohne-website-maschinenbau.de", "facebook-maschinen.de"}
    assert job.count == 2 and job.progress["crawled"] == 4


async def test_guess_skips_domain_rejected_by_search(tmp_path: Path, search_mock) -> None:
    """Default order email_domain → search → guess: when the search result is the guessed domain and
    fails the identity check, the guess does not crawl it a second time."""
    search_mock.serp("kranich maschinenbau", [(f"{GUESSED}/", "Kranich Maschinenbau München")])
    deps, jobs = make_deps(tmp_path)
    with_search(deps, search_mock)
    body = "<p>Kranich Maschinenbau GmbH</p><p>Leopoldstr. 5</p><p>80331 München</p>"
    with respx.mock(assert_all_mocked=True) as mock:
        search_mock.install(mock)
        mock_sites(mock, [lookup_node(1, "Kranich Maschinenbau")], guessed_sites(body))
        job = await run_job(deps, jobs, BODY)
        homes = [c for c in mock.calls if str(c.request.url) == f"{GUESSED}/"]
    assert search_mock.queries and job.count == 0 and job.progress["crawled"] == 0
    assert len(homes) == 1


# --- source-agnostic region check + area gazetteer ---------------------------------------------------
class FakeAdapter:
    """A non-OSM ``SourceAdapter``: yields its candidates for the given area ids."""

    name, countries, daily_budget = "fake", None, None

    def __init__(self, by_area: dict[str, list[CompanyCandidate]]) -> None:
        self.by_area = by_area

    async def discover(self, slice_):
        for cand in self.by_area.get(slice_.area_id, []):
            yield cand


def with_adapter(deps, fake: FakeAdapter):
    from leadscraper.sources.registry import build_adapters
    deps.adapter_factory = lambda settings, profile, **kw: build_adapters(settings, profile, **kw) + [fake]
    return deps


def is_gazetteer(request: httpx.Request) -> bool:
    return '"boundary"="postal_code"' in parse_qs(request.content.decode())["data"][0]


def mock_region(mock: respx.MockRouter, elements, sites: dict[str, str], gazetteer=None):
    """Like ``mock_sites``; gazetteer queries get ``gazetteer`` (a JSON body, or a callable ``query
    -> Response``)."""
    def overpass(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["data"][0]
        if is_gazetteer(request):
            if callable(gazetteer):
                return gazetteer(query)
            return httpx.Response(200, json=gazetteer or {"elements": []})
        els = elements
        if isinstance(elements, dict):
            els = next((v for code, v in elements.items() if f'"ISO3166-2"="{code}"' in query or
                        f'"ISO3166-1"="{code}"' in query), [])
        return httpx.Response(200, json={"elements": els})

    route = mock.post(OVERPASS).mock(side_effect=overpass)
    mock.get(url__regex=r"https?://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))

    def site(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in sites:
            return httpx.Response(200, headers=HTML, text=sites[url])
        return httpx.Response(404, headers=HTML, text="<html>404</html>")

    mock.get(url__regex=r"https?://.*").mock(side_effect=site)
    return route


def web_company(slug: str, main: str, footer: str = "", source: str = "web_search",
                **hints: str) -> tuple[CompanyCandidate, dict[str, str]]:
    """A non-OSM candidate with its own website (home → Impressum)."""
    base = f"https://{slug}-example.de"
    name = f"{slug.title()} Maschinenbau"
    sites = {f"{base}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{base}/impressum": (f"<html><body><main><p>{name} GmbH</p>{main}<p>info@{slug}-example.de</p>"
                                   f"</main><footer>{footer}</footer></body></html>")}
    return CompanyCandidate(name=name, source=source, source_ref=f"{source}/{slug}", website=base,
                            hints=hints), sites


def osm_tagged(i: int, slug: str, postcode: str = "40210") -> tuple[dict, dict[str, str]]:
    base = f"https://{slug}-example.de"
    sites = {f"{base}/": '<html lang="de"><a href="/impressum">Impressum</a></html>',
             f"{base}/impressum": f"<html><body><p>{slug.title()} GmbH</p><p>info@{slug}-example.de</p></body></html>"}
    return node(i, f"{slug.title()} Maschinen", base, **{"addr:postcode": postcode,
                                                         "addr:city": "Düsseldorf"}), sites


IN_AREA = "<p>Werkstr. 1</p><p>40210 Düsseldorf</p>"
OUT_OF_AREA = "<p>Isarweg 1</p><p>80331 München</p>"
NRW_ID = "iso:DE-NW"


async def test_non_osm_candidate_with_trustworthy_location_accepted(tmp_path: Path) -> None:
    """a fake register adapter declaring ``area_match="source"`` next to
    OSM → its company is in the result with the request's region/industry labels (rule 2)."""
    osm, osm_sites = osm_tagged(1, "osmfirma")
    reg, reg_sites = web_company("register", "<p>Telefon 0211 1234</p>", source="fake_register",
                                 area_match="source")
    deps, jobs = make_deps(tmp_path)
    with_adapter(deps, FakeAdapter({NRW_ID: [reg]}))
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock_region(mock, [osm], {**osm_sites, **reg_sites})
        job = await run_job(deps, jobs, BODY)
        gazetteer_calls = [c for c in route.calls if is_gazetteer(c.request)]
    assert job.status is JobStatus.SUCCESS
    by_site = {c["website"]: c for c in job.result}
    assert by_site["https://register-example.de"] == {
        "company_name": "Register Maschinenbau GmbH", "company_email": "info@register-example.de",
        "website": "https://register-example.de", "country": "Germany", "region": "NRW",
        "industry": "Maschinenbau"}
    assert "https://osmfirma-example.de" in by_site and gazetteer_calls == []


@pytest.mark.parametrize("main,footer,accepted,gazetteer_queries", [
    (IN_AREA, "", True, 0),                       # decided by the job's OSM postcodes
    (OUT_OF_AREA, "", False, 1),                  # gazetteer consulted; the mismatch is final
    ("<p>Telefon 0211 1234</p>", "", False, 0),   # no own address → no evidence, no query
], ids=["in-area", "out-of-area", "no-address"])
async def test_web_search_source_goes_through_address_rule(tmp_path: Path, main: str, footer: str,
                                                           accepted: bool, gazetteer_queries: int) -> None:
    """A ``web_search`` candidate (no ``area_match``, no coordinates) is accepted only through rule
    3, the company's own address."""
    osm, osm_sites = osm_tagged(1, "osmfirma")
    web, web_sites = web_company("websuche", main, footer)
    deps, jobs = make_deps(tmp_path)
    with_adapter(deps, FakeAdapter({NRW_ID: [web]}))
    gaz = {"elements": [{"type": "relation", "id": 9, "tags": {"boundary": "postal_code", "postal_code": "40210"}}]}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock_region(mock, [osm], {**osm_sites, **web_sites}, gaz)
        job = await run_job(deps, jobs, BODY)
        queries = [c for c in route.calls if is_gazetteer(c.request)]
    sites = {c["website"] for c in job.result}
    assert ("https://websuche-example.de" in sites) is accepted and "https://osmfirma-example.de" in sites
    assert len(queries) == gazetteer_queries


async def test_postcode_only_in_footer_list_rejected(tmp_path: Path) -> None:
    """The in-area postcode appears only in the legal page's footer (a branch list) → not the
    company's own address → rejected, although 40210 is one of the job's OSM postcodes."""
    osm, osm_sites = osm_tagged(1, "osmfirma")
    web, web_sites = web_company("fusszeile", "<p>Telefon 0211 1234</p>",
                                 footer="<p>Standorte:</p>" + IN_AREA + "<p>Zeil 1</p><p>60311 Frankfurt</p>")
    deps, jobs = make_deps(tmp_path)
    with_adapter(deps, FakeAdapter({NRW_ID: [web]}))
    with respx.mock(assert_all_mocked=True) as mock:
        mock_region(mock, [osm], {**osm_sites, **web_sites})
        job = await run_job(deps, jobs, BODY)
    assert {c["website"] for c in job.result} == {"https://osmfirma-example.de"}


async def test_sparse_area_zero_osm_candidates_gazetteer_evidence_accepted(tmp_path: Path) -> None:
    web, web_sites = web_company("duenn", IN_AREA)
    deps, jobs = make_deps(tmp_path)
    with_adapter(deps, FakeAdapter({NRW_ID: [web]}))
    gaz = {"elements": [{"type": "relation", "id": 9, "tags": {"boundary": "postal_code", "postal_code": "40210"}},
                        {"type": "node", "id": 10, "tags": {"place": "city", "name": "Düsseldorf"}}]}
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock_region(mock, [], web_sites, gaz)
        job = await run_job(deps, jobs, BODY)
        queries = [c for c in route.calls if is_gazetteer(c.request)]
    assert job.count == 1 and job.result[0]["website"] == "https://duenn-example.de"
    assert job.result[0]["region"] == "NRW" and len(queries) == 1


async def test_gazetteer_queried_once_per_area_and_counts_toward_budget(tmp_path: Path) -> None:
    before = metrics.REGISTRY.get_sample_value("source_budget_used", {"source": "osm"}) or 0.0
    a, a_sites = web_company("erstens", IN_AREA)
    b, b_sites = web_company("zweitens", "<p>Kaiserstr. 2</p><p>40211 Düsseldorf</p>")
    deps, jobs = make_deps(tmp_path)
    with_adapter(deps, FakeAdapter({NRW_ID: [a, b]}))
    gaz = {"elements": [{"type": "relation", "id": 9, "tags": {"boundary": "postal_code", "postal_code": "40210"}},
                        {"type": "relation", "id": 8, "tags": {"boundary": "postal_code", "postal_code": "40211"}}]}
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock_region(mock, [], {**a_sites, **b_sites}, gaz)
        job = await run_job(deps, jobs, BODY)
        posts, queries = route.call_count, [c for c in route.calls if is_gazetteer(c.request)]
    assert job.count == 2 and len(queries) == 1 and posts == 2              # slice + one gazetteer
    after = metrics.REGISTRY.get_sample_value("source_budget_used", {"source": "osm"})
    assert after == before + 2


async def test_gazetteer_failure_falls_back_and_warns_once(tmp_path: Path) -> None:
    """NRW's gazetteer answers with a runtime-error remark, Hessen's with HTTP 400: the check falls
    back to the job's OSM postcodes (A: 40210 ✓, B: 40211 ✗) and area names (C in Hessen ✗), and
    the job carries one gazetteer warning."""
    osm, osm_sites = osm_tagged(1, "osmfirma")
    a, a_sites = web_company("ahorn", IN_AREA)
    b, b_sites = web_company("birke", "<p>Kaiserstr. 2</p><p>40211 Düsseldorf</p>")
    c, c_sites = web_company("carya", "<p>Zeil 1</p><p>60311 Frankfurt am Main</p>")

    def gazetteer_answer(query: str) -> httpx.Response:
        if '"DE-NW"' in query:
            return httpx.Response(200, json={"elements": [], "remark": "runtime error: Query timed out"})
        return httpx.Response(400, text="bad request")

    deps, jobs = make_deps(tmp_path)
    with_adapter(deps, FakeAdapter({NRW_ID: [a, b], "iso:DE-HE": [c]}))
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock_region(mock, {"DE-NW": [osm], "DE-HE": []}, {**osm_sites, **a_sites, **b_sites, **c_sites},
                            gazetteer_answer)
        job = await run_job(deps, jobs, {**BODY, "regions": ["NRW", "Hessen"]})
        queries = [c for c in route.calls if is_gazetteer(c.request)]
    assert job.status is JobStatus.SUCCESS
    assert {r["website"] for r in job.result} == {"https://osmfirma-example.de", "https://ahorn-example.de"}
    assert len(queries) == 2
    warnings = [w for w in job.resolved["warnings"] if "gazetteer" in w]
    assert len(warnings) == 1
    assert "runtime error at overpass.test" in warnings[0] or "HTTPStatusError 400" in warnings[0]


async def test_no_gazetteer_for_country_level_area(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsplit country slice (level "country") sends no gazetteer query; the check uses the job's
    OSM postcodes only (A: 40210 ✓, B: 80331 ✗)."""
    from leadscraper import constants as C
    monkeypatch.setattr(C, "COUNTRY_SPLIT_MAX_SUBDIVISIONS", 0)            # keep the country slice
    osm, osm_sites = osm_tagged(1, "osmfirma")
    a, a_sites = web_company("ahorn", IN_AREA)
    b, b_sites = web_company("birke", OUT_OF_AREA)
    deps, jobs = make_deps(tmp_path)
    with_adapter(deps, FakeAdapter({"iso:DE": [a, b]}))
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock_region(mock, [osm], {**osm_sites, **a_sites, **b_sites})
        job = await run_job(deps, jobs, {**BODY, "regions": []})
        bodies = [parse_qs(c.request.content.decode())["data"][0] for c in route.calls]
    assert job.status is JobStatus.SUCCESS
    assert len(bodies) == 1 and 'area["ISO3166-1"="DE"]' in bodies[0]       # the one country slice
    assert {r["website"] for r in job.result} == {"https://osmfirma-example.de", "https://ahorn-example.de"}


async def test_osm_region_path_unchanged(tmp_path: Path) -> None:
    """Pure-OSM jobs send no gazetteer query; the golden counters are unchanged."""
    deps, jobs = make_deps(tmp_path)
    with respx.mock(assert_all_mocked=True) as mock:
        overpass = mock_web(mock)
        job = await run_job(deps, jobs, BODY)
        bodies = [parse_qs(c.request.content.decode())["data"][0] for c in overpass.calls]
    assert len(bodies) == 1 and '"boundary"="postal_code"' not in bodies[0]
    assert job.count == 2 and job.progress["crawled"] == 4
