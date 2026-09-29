"""Web-search discovery of companies not in OSM."""

import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from leadscraper.domain.models import JobStatus
from leadscraper.jobs.manager import JobManager
from leadscraper.observability import metrics
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.services.scrape_service import PipelineDeps, ScrapePipeline, runner_for
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import OverpassGate
from leadscraper.sources.registry import enabled_source_names
from leadscraper.sources.web_search import DuckDuckGoHtml, SearchGate
from leadscraper.verification import lists

pytestmark = pytest.mark.anyio

OVERPASS = "https://overpass.test/api/interpreter"
HTML = {"content-type": "text/html; charset=utf-8"}
BODY = {"country": "Germany", "regions": ["NRW"], "industries": ["Maschinenbau"],
        "information": ["company_name", "company_email", "website"], "max_output": 10}
DISCOVERY = "maschinenbau nordrhein-westfalen"          # the first discovery query of the slice
IN_AREA = "<p>Werkstr. 1</p><p>40210 Düsseldorf</p>"
GAZETTEER = {"elements": [
    {"type": "relation", "id": 1, "tags": {"boundary": "postal_code", "postal_code": "40210"}},
    {"type": "relation", "id": 2, "tags": {"boundary": "postal_code", "postal_code": "40211"}},
    {"type": "node", "id": 3, "tags": {"place": "city", "name": "Düsseldorf"}}]}


async def public_dns(_host: str) -> list[str]:
    return ["93.184.216.34"]


def make_deps(tmp_path: Path, search_mock=None, **env: str) -> tuple[PipelineDeps, JobManager]:
    settings = load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "OVERPASS_URL": OVERPASS,
                              "CRAWLER_PER_DOMAIN_DELAY_S": "0", **env})
    supp = tmp_path / "suppression.txt"
    supp.write_text("", encoding="utf-8")
    deps = PipelineDeps(settings=settings, resolver=Resolver(settings),
                        gate=OverpassGate(per_minute=600_000), verifier_factory=lambda s: None,
                        dns_resolve=public_dns, suppression=lists.SuppressionList(path=supp))
    deps.website_guess = False                  # guesses are not what these tests look at
    if search_mock is not None:
        deps.search_backend, deps.search_gate = DuckDuckGoHtml(), search_mock.gate
    return deps, JobManager(settings.temp_dir)


def node(i: int, name: str, website: str | None = None, postcode: str = "40210",
         city: str = "Düsseldorf") -> dict:
    tags = {"name": name, "addr:postcode": postcode, "addr:city": city}
    if website:
        tags["website"] = website
    return {"type": "node", "id": i, "lat": 51.2, "lon": 6.8, "tags": tags}


def site(slug: str, main: str, *, footer: str = "", home: str = "") -> tuple[str, dict[str, str]]:
    """A company site (home → Impressum); ``main`` is the Impressum body before the email."""
    base = f"https://{slug}-example.de"
    return base, {
        f"{base}/": f'<html lang="de"><body><p>{home}</p><a href="/impressum">Impressum</a></body></html>',
        f"{base}/impressum": (f"<html><body><main>{main}<p>E-Mail: info@{slug}-example.de</p></main>"
                              f"<footer>{footer}</footer></body></html>")}


def is_gazetteer(query: str) -> bool:
    return '"boundary"="postal_code"' in query


def mock_world(mock: respx.MockRouter, search_mock, elements, sites: dict[str, str], *,
               gazetteer: dict | None = None, overpass_delay: float = 0.0, on_overpass=None,
               site_delay: float = 0.0):
    """Search hosts first (``search_mock``), then Overpass and the company sites."""
    if search_mock is not None:
        search_mock.install(mock)

    async def overpass(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["data"][0]
        if is_gazetteer(query):
            return httpx.Response(200, json=gazetteer or {"elements": []})
        if overpass_delay:
            await asyncio.sleep(overpass_delay)
        if on_overpass is not None:
            on_overpass()
        els = elements(query) if callable(elements) else elements
        return httpx.Response(200, json={"elements": els})

    route = mock.post(OVERPASS).mock(side_effect=overpass)
    mock.get(url__regex=r"https?://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))

    async def page(request: httpx.Request) -> httpx.Response:
        if site_delay:
            await asyncio.sleep(site_delay)
        url = str(request.url)
        if url in sites:
            return httpx.Response(200, headers=HTML, text=sites[url])
        return httpx.Response(404, headers=HTML, text="<html>404</html>")

    mock.get(url__regex=r"https?://.*").mock(side_effect=page)
    return route


async def run_job(deps, jobs: JobManager, body: dict):
    job, _ = jobs.create("scrape", body, target=body["max_output"])
    await jobs.start(job, runner_for(deps, jobs))
    return job


def resolved(method: str) -> float:
    return metrics.REGISTRY.get_sample_value("websites_resolved_total", {"method": method}) or 0.0


def discovery_queries(search_mock) -> list[str]:
    return [q for q in search_mock.queries if '"' not in q]


def lookup_queries(search_mock) -> list[str]:
    return [q for q in search_mock.queries if '"' in q]


# --- region: rule 3 for web candidates -----------------------------------------------------------------
async def test_web_candidate_with_in_area_legal_address_accepted(tmp_path: Path, search_mock) -> None:
    base, pages = site("nordwerk", "<p>Nordwerk Maschinenbau GmbH</p>" + IN_AREA)
    search_mock.serp(DISCOVERY, [(f"{base}/", "Nordwerk – Maschinen aus Düsseldorf | Startseite")])
    before = resolved("web_discovery")
    found = metrics.REGISTRY.get_sample_value("candidates_discovered_total",
                                              {"country": "DE", "source": "web_search"}) or 0.0
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [], pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS
    assert job.resolved["sources"] == ["osm", "web_search"]
    assert job.result == [{"company_name": "Nordwerk Maschinenbau GmbH",      # legal name, not the SERP title
                           "company_email": "info@nordwerk-example.de", "website": base,
                           "country": "Germany", "region": "NRW", "industry": "Maschinenbau"}]
    assert resolved("web_discovery") == before + 1
    assert metrics.REGISTRY.get_sample_value("candidates_discovered_total",
                                             {"country": "DE", "source": "web_search"}) == found + 1


async def test_web_candidate_out_of_area_postcode_excluded(tmp_path: Path, search_mock) -> None:
    base, pages = site("isarwerk", "<p>Isarwerk Maschinenbau GmbH</p><p>Isarweg 1</p><p>80331 München</p>")
    search_mock.serp(DISCOVERY, [(f"{base}/", "Isarwerk Maschinenbau")])
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock_world(mock, search_mock, [], pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
        gazetteer_calls = [c for c in route.calls if is_gazetteer(parse_qs(c.request.content.decode())["data"][0])]
    assert job.status is JobStatus.SUCCESS and job.count == 0 and len(gazetteer_calls) == 1


async def test_web_candidate_postcode_only_in_footer_excluded(tmp_path: Path, search_mock) -> None:
    base, pages = site("netzwerk", "<p>Netzwerk Maschinenbau GmbH</p><p>Geschäftsführer: M. Muster</p>",
                       footer="<p>Standorte:</p>" + IN_AREA)
    search_mock.serp(DISCOVERY, [(f"{base}/", "Netzwerk Maschinenbau")])
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [], pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 0


# ---  ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("jsonld,accepted", [
    ("", False),
    ('<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Organization", '
     '"name": "Nordwerk Maschinenbau"}</script>', True),
], ids=["no-name", "jsonld-name"])
async def test_web_candidate_without_legal_name_excluded(tmp_path: Path, search_mock, jsonld: str,
                                                         accepted: bool) -> None:
    """(a) The page has no legal-name line (no legal form); the SERP title has one but is never
    used."""
    base, pages = site("nordwerk", f"{jsonld}<p>Nordwerk Maschinenbau</p>" + IN_AREA)
    search_mock.serp(DISCOVERY, [(f"{base}/", "Nordwerk Maschinenbau GmbH")])
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [], pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
    assert job.count == int(accepted)
    if accepted:
        assert job.result[0]["company_name"] == "Nordwerk Maschinenbau"


async def test_web_candidate_non_industry_site_excluded(tmp_path: Path, search_mock) -> None:
    """(b) An in-area company with a legal name, but no industry keyword on its home/legal page."""
    base, pages = site("blumenhaus", "<p>Blumenhaus Sonnenschein GmbH</p>" + IN_AREA,
                       home="Frische Blumen und Gestecke für jeden Anlass")
    search_mock.serp(DISCOVERY, [(f"{base}/", "Blumenhaus Sonnenschein")])
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [], pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 0


async def test_directory_results_never_crawled(tmp_path: Path, search_mock) -> None:
    """(c) Directory/social results are filtered before any crawl."""
    base, pages = site("nordwerk", "<p>Nordwerk Maschinenbau GmbH</p>" + IN_AREA)
    search_mock.serp(DISCOVERY, [("https://www.gelbeseiten.de/branchen/maschinenbau/nrw", "Maschinenbau in NRW"),
                                 ("https://www.facebook.com/nordwerk", "Nordwerk | Facebook"),
                                 (f"{base}/", "Nordwerk Maschinenbau")])
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [], pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
        hosts = {c.request.url.host for c in mock.calls}
    assert job.count == 1 and job.result[0]["website"] == base
    assert not hosts & {"www.gelbeseiten.de", "gelbeseiten.de", "www.facebook.com", "facebook.com"}


# --- merge with OSM (step 6) ------------------------------------------------------------------------------
@pytest.mark.parametrize("overpass_delay", [0.0, 0.3], ids=["osm-first", "web-first"])
async def test_web_and_osm_same_domain_merged_once(tmp_path: Path, search_mock, overpass_delay: float) -> None:
    """(a) The web result is the OSM company's own domain → one record (the OSM one), one crawl —
    also when the web first pass is answered before Overpass (the OSM entry still wins)."""
    base, pages = site("anker", "<p>Anker Maschinenbau GmbH</p>" + IN_AREA)
    search_mock.serp(DISCOVERY, [("https://www.anker-example.de/", "Anker Maschinenbau – Startseite")])
    before_osm, before_web = resolved("osm_tag"), resolved("web_discovery")
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [node(1, "Anker Maschinenbau", base)], pages,
                   gazetteer=GAZETTEER, overpass_delay=overpass_delay)
        job = await run_job(deps, jobs, BODY)
        homes = [c for c in mock.calls if c.request.url.path == "/" and "anker" in c.request.url.host]
    assert discovery_queries(search_mock)                        # the web result was really there
    assert [c["website"] for c in job.result] == [base] and len(homes) == 1
    assert job.progress["candidates"] == 1                       # the web result never became an entry
    assert resolved("osm_tag") == before_osm + 1 and resolved("web_discovery") == before_web


def two_industries(query: str, machinery: list[dict]) -> list[dict]:
    return machinery if "maschinenfabrik" in query else []   # only the Maschinenbau slice has OSM data


async def test_web_candidate_matching_osm_entry_by_name_and_postcode_merged(tmp_path: Path,
                                                                             search_mock) -> None:
    """(b) A website-less OSM entry (Maschinenbau slice) and a web result (Metallbau slice) with the
    same legal name + postcode, verified by the identity check → one record with the OSM entry's
    labels and the web result's website; no lookup search for that entry."""
    base, pages = site("kranich", "<p>Kranich Maschinenbau GmbH</p>" + IN_AREA,
                       home="Maschinenbau und Metallbau aus Düsseldorf")
    search_mock.serp("metallbau nordrhein-westfalen", [(f"{base}/", "Kranich – Metallbau NRW")])
    search_mock.serp('"kranich maschinenbau"', [(f"{base}/", "Kranich Maschinenbau")])   # must not be asked
    before = resolved("web_discovery_merged")
    deps, jobs = make_deps(tmp_path, search_mock)
    osm = [node(1, "Kranich Maschinenbau")]
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, lambda q: two_industries(q, osm), pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, {**BODY, "industries": ["Maschinenbau", "Metallbau"]})
    assert job.status is JobStatus.SUCCESS
    assert job.result == [{"company_name": "Kranich Maschinenbau GmbH",
                           "company_email": "info@kranich-example.de", "website": base,
                           "country": "Germany", "region": "NRW", "industry": "Maschinenbau"}]
    assert resolved("web_discovery_merged") == before + 1
    assert lookup_queries(search_mock) == []


async def test_web_duplicate_of_accepted_osm_company_dropped(tmp_path: Path, search_mock) -> None:
    """The OSM company (own website, accepted) and a web result with another domain but the same
    legal name + postcode → the web record is dropped."""
    osm_base, osm_pages = site("kranich-osm", "<p>Kranich Maschinenbau GmbH</p>" + IN_AREA)
    web_base, web_pages = site("kranich-web", "<p>Kranich Maschinenbau GmbH</p>" + IN_AREA)
    search_mock.serp(DISCOVERY, [(f"{web_base}/", "Kranich Maschinenbau")])
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [node(1, "Kranich Maschinenbau", osm_base)],
                   {**osm_pages, **web_pages}, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
        crawled_web = any("kranich-web" in c.request.url.host for c in mock.calls)
    assert crawled_web and [c["website"] for c in job.result] == [osm_base]


async def test_generic_name_web_result_not_merged_into_other_osm_entry(tmp_path: Path,
                                                                        search_mock) -> None:
    """"Maschinenbau Düsseldorf" (OSM, no website, generic tokens only) vs the web result "Nord
    Maschinenbau Düsseldorf GmbH", same postcode: the dedup rule matches, the identity check for
    the OSM entry does not → no merge; the web result is a new record (case c)."""
    base, pages = site("nordmb", "<p>Nord Maschinenbau Düsseldorf GmbH</p>" + IN_AREA)
    search_mock.serp(DISCOVERY, [(f"{base}/", "Nord Maschinenbau Düsseldorf")])
    before_merged, before_web = resolved("web_discovery_merged"), resolved("web_discovery")
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True) as mock:
        mock_world(mock, search_mock, [node(1, "Maschinenbau Düsseldorf")], pages, gazetteer=GAZETTEER)
        job = await run_job(deps, jobs, BODY)
    assert [c["company_name"] for c in job.result] == ["Nord Maschinenbau Düsseldorf GmbH"]
    assert resolved("web_discovery_merged") == before_merged and resolved("web_discovery") == before_web + 1


# ---  -----------------------------------------------------------------------------------------------------
async def test_first_discovery_query_concurrent_with_overpass(tmp_path: Path, search_mock) -> None:
    """The first discovery query is sent before the (slow) Overpass slice answers."""
    seen_at_overpass: list[int] = []
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_world(mock, search_mock, [], {}, overpass_delay=0.5,
                   on_overpass=lambda: seen_at_overpass.append(len(discovery_queries(search_mock))))
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS
    assert seen_at_overpass and seen_at_overpass[0] >= 1
    assert discovery_queries(search_mock)[0] == "maschinenbau Nordrhein-Westfalen"


async def test_discovery_budget_share(tmp_path: Path, search_mock) -> None:
    """8 slices × 3 queries would be 24; discovery stops at ⅓ of the 60-query job budget = 20."""
    regions = ["NRW", "Hessen", "Bayern", "Niedersachsen", "Sachsen", "Berlin", "Hamburg", "Bremen"]
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_world(mock, search_mock, [], {})
        job = await run_job(deps, jobs, {**BODY, "regions": regions, "max_output": 50})
    assert job.status is JobStatus.SUCCESS and job.count == 0
    assert len(discovery_queries(search_mock)) == 20 and lookup_queries(search_mock) == []


@pytest.mark.parametrize("target_met", [True, False])
async def test_further_discovery_queries_only_in_top_up(tmp_path: Path, search_mock, target_met: bool) -> None:
    """Target met by OSM → at most the first-pass query; target not met → the slice's further
    queries follow in top-up rounds (3 in total, next keywords)."""
    elements, pages = [], {}
    for i in range(1, 3):
        base, p = site(f"osm{i}", f"<p>Osm{i} Maschinenbau GmbH</p>" + IN_AREA)
        elements.append(node(i, f"Osm{i} Maschinenbau", base))
        pages.update(p)
    deps, jobs = make_deps(tmp_path, search_mock)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_world(mock, search_mock, elements, pages)
        job = await run_job(deps, jobs, {**BODY, "max_output": 2 if target_met else 5})
    assert job.count == 2
    if target_met:
        assert len(discovery_queries(search_mock)) <= 1
    else:
        assert discovery_queries(search_mock) == ["maschinenbau Nordrhein-Westfalen",
                                                  "maschinenfabrik Nordrhein-Westfalen",
                                                  "anlagenbau Nordrhein-Westfalen"]


async def test_stop_cancels_pending_search_job_not_delayed(tmp_path: Path, search_mock) -> None:
    """Production search spacing (3 s): the target is met by OSM (after discovery; slow site) while
    the first discovery query still waits behind the backend robots.txt request → the job ends at
    once, the query never runs, and the pending search task is cancelled."""
    base, pages = site("schnell", "<p>Schnell Maschinenbau GmbH</p>" + IN_AREA)
    deps, jobs = make_deps(tmp_path, search_mock)
    deps.search_gate = SearchGate()
    pipelines: list[ScrapePipeline] = []

    async def runner(job) -> None:                           # keeps the pipeline for the checks
        pipelines.append(pipeline := ScrapePipeline(deps, jobs, job))
        await pipeline.run()

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_world(mock, search_mock, [node(1, "Schnell Maschinenbau", base)], pages, site_delay=0.2)
        job, _ = jobs.create("scrape", {**BODY, "max_output": 1}, target=1)
        task = jobs.start(job, runner)
        target_at = None
        while not task.done():
            await asyncio.wait({task}, timeout=0.02)
            if target_at is None and job.count >= 1:
                target_at = time.monotonic()
        ended = time.monotonic()
        await task
        await asyncio.sleep(0)
        web_tasks = pipelines[0].web_tasks
    assert job.status is JobStatus.SUCCESS and job.count == 1 and target_at is not None
    assert ended - target_at < 1.0 and discovery_queries(search_mock) == []
    assert web_tasks and all(t.cancelled() for t in web_tasks)   # pending search work was cancelled


# --- / registration ---------------------------------------------------------------------------------------
async def test_off_switch_disables_search_and_discovery(tmp_path: Path) -> None:
    """``WEB_SEARCH_URL=off`` (the app then builds no backend): no search request at all — the
    conftest guard fails any — and ``resolved.sources == ["osm"]``."""
    base, pages = site("nordwerk", "<p>Nordwerk Maschinenbau GmbH</p>" + IN_AREA)
    deps, jobs = make_deps(tmp_path, None, WEB_SEARCH_URL="off")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock_world(mock, None, [node(1, "Nordwerk Maschinenbau", base), node(2, "Ohne Website Maschinenbau")],
                   pages)
        job = await run_job(deps, jobs, BODY)
    assert job.status is JobStatus.SUCCESS and job.count == 1
    assert job.resolved["sources"] == ["osm"]


@pytest.mark.parametrize("env,web", [({}, True), ({"WEB_SEARCH_URL": "off"}, False)], ids=["default", "off"])
def test_enabled_sources_consistent(env: dict, web: bool) -> None:
    settings = load_settings(env)
    enabled = enabled_source_names(settings)
    profile = Resolver(settings).profiles.get("DE")
    assert set(profile.sources) <= set(enabled)
    assert ("web_search" in enabled) is web and ("web_search" in profile.sources) is web
