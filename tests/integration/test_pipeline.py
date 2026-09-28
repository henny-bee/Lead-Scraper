"""End-to-end scrape pipeline with mocked Overpass + mocked company websites (respx) and fake DNS.
No network: HTTP is fully mocked (assert_all_mocked), DNS lookups use fakes."""

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import dns.name
import dns.resolver
import httpx
import pytest
import respx

from leadscraper.domain.models import JobStatus
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
    settings = load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "OVERPASS_URL": OVERPASS,
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
    assert 'area["ISO3166-1"="DE"][admin_level=2]' in query


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
        jobs.fail(job.job_id, "job_timeout", "too slow")       # what the sweeper does (T05) …
        await jobs.cancel_task(job)                              # … followed by cancellation
    assert job.status is JobStatus.FAILED and job.error["code"] == "job_timeout"
    assert not job.candidates_path.exists() and not job.crawl_dir.exists()
    assert not job.result_path.exists()
