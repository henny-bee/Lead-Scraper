"""Web search backends, SearchGate and the test guard."""

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from leadscraper import constants as C
from leadscraper.observability import metrics
from leadscraper.settings import load_settings
from leadscraper.sources.web_search import (
    DuckDuckGoHtml,
    SearchBlocked,
    SearchGate,
    SearchJob,
    SearchResult,
    SearxngJson,
    build_search_backend,
    decode_ddg_href,
    parse_ddg_html,
    parse_searxng_json,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "search"


ROOT = Path(__file__).resolve().parents[2]
DDG = C.DUCKDUCKGO_HTML_URL


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --- parsers ---------------------------------------------------------------------------------------
def test_parse_ddg_results_organic_decoded_ads_skipped_directories_filtered_max_10() -> None:
    results = parse_ddg_html(fixture("ddg_results.html"))
    urls = [r.url for r in results]
    assert len(results) == C.WEB_SEARCH_MAX_RESULTS == 10
    assert urls[0] == "https://kranich-transporte-example.de"            # uddg decoded, origin only
    assert results[0].title.startswith("Kranich Transporte GmbH") and results[0].snippet
    assert not any("anzeige" in u for u in urls)                           # ad skipped
    assert not any(d in u for u in urls for d in ("gelbeseiten", "northdata", "facebook"))
    assert "http://10.0.0.1" not in urls                                   # private literal dropped
    assert urls.count("https://kranich-transporte-example.de") == 1        # deduplicated origin
    assert urls[1:] == [f"https://firma-{i:02d}-example.de" for i in range(1, 10)]


def test_parse_malformed_or_empty() -> None:
    assert parse_ddg_html("<html><body><div class='results'><a class=result__a>") == []
    assert parse_ddg_html("") == [] and parse_ddg_html("not html at all") == []
    assert parse_searxng_json("{not json") == [] and parse_searxng_json('{"results": 5}') == []


def test_decode_ddg_href() -> None:
    assert decode_ddg_href("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa-example.de%2Fx&rut=1") == "https://a-example.de/x"
    assert decode_ddg_href("https://b-example.de/") == "https://b-example.de/"
    assert decode_ddg_href("//duckduckgo.com/y.js?ad_provider=x") is None
    assert decode_ddg_href("javascript:void(0)") is None


def test_parse_searxng_results() -> None:
    results = parse_searxng_json(fixture("searxng_results.json"))
    assert [r.url for r in results] == ["https://kranich-transporte-example.de", "https://firma-01-example.de"]


# --- settings / backend selection --------------------------------------------------------------------
def test_default_backend_is_duckduckgo() -> None:
    settings = load_settings({})
    assert settings.web_search_url == C.DUCKDUCKGO_HTML_URL and settings.web_search_backend == "duckduckgo"
    assert isinstance(build_search_backend(settings), DuckDuckGoHtml)
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"WEB_SEARCH_URL={C.DUCKDUCKGO_HTML_URL}" in example and "off" in example


def test_empty_value_means_default() -> None:
    assert load_settings({"WEB_SEARCH_URL": ""}).web_search_url == C.DUCKDUCKGO_HTML_URL
    assert load_settings({"WEB_SEARCH_URL": "  "}).web_search_enabled


def test_off_switch_builds_no_backend() -> None:
    for value in ("off", "OFF", " Off "):
        settings = load_settings({"WEB_SEARCH_URL": value})
        assert not settings.web_search_enabled and build_search_backend(settings) is None


def test_other_url_is_searxng() -> None:
    backend = build_search_backend(load_settings({"WEB_SEARCH_URL": "http://searxng.test/"}))
    assert isinstance(backend, SearxngJson) and backend.search_url == "http://searxng.test/search"


@pytest.mark.parametrize("value", ["https://www.google.com/search", "https://google.de", "https://www.bing.com/search",
                                   "https://bing.com", "ftp://searxng.test", "no url", "http://"])
def test_google_and_bing_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        load_settings({"WEB_SEARCH_URL": value})


# --- gate ----------------------------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds
        await asyncio.sleep(0)


class FakeBackend:
    name = "fake"
    url = "https://search.fake-example.de/html/"
    user_agent = "LeadScraperBot/test"

    def __init__(self, robots: tuple[int | None, str] = (404, ""), block: int | None = None) -> None:
        self.robots, self.block = robots, block
        self.robots_calls = self.calls = self.in_flight = self.max_in_flight = 0

    async def fetch_robots(self):
        self.robots_calls += 1
        return self.robots

    async def search(self, query: str, *, language: str, country: str):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        if self.block is not None and self.calls >= self.block:
            raise SearchBlocked("http 429")
        return [SearchResult(f"https://{query}-example.de", query)]


def gate_with(clock: FakeClock, **kw) -> SearchGate:
    return SearchGate(clock=clock, sleep=clock.sleep, **kw)


@pytest.mark.anyio
async def test_gate_single_flight_and_spacing() -> None:
    clock, backend = FakeClock(), FakeBackend()
    gate, job = gate_with(clock, min_interval_s=3.0), SearchJob()
    results = await asyncio.gather(*(gate.search(backend, q, language="de", country="DE", job=job)
                                     for q in ("a", "b", "c")))
    assert [r[0].url for r in results] == ["https://a-example.de", "https://b-example.de", "https://c-example.de"]
    assert backend.max_in_flight == 1 and gate.max_in_flight == 1
    # robots.txt first, then 3 searches: each request waits until 3 s after the previous one
    assert clock.slept == [3.0, 3.0, 3.0] and backend.robots_calls == 1 and job.used == 3


@pytest.mark.anyio
async def test_budget_and_daily_cap() -> None:
    clock, backend = FakeClock(), FakeBackend()
    gate = gate_with(clock, min_interval_s=0.0, daily_budget=3)
    job = SearchJob(budget=2)
    before = metrics.REGISTRY.get_sample_value("source_budget_used", {"source": "web_search"}) or 0.0
    assert [len(await gate.search(backend, q, language="de", country="DE", job=job)) for q in "abc"] == [1, 1, 0]
    assert job.used == 2 and any("budget" in w for w in job.warnings)
    other = SearchJob()
    assert [len(await gate.search(backend, q, language="de", country="DE", job=other)) for q in "de"] == [1, 0]
    assert backend.calls == 3 and any("Daily" in w for w in other.warnings)
    after = metrics.REGISTRY.get_sample_value("source_budget_used", {"source": "web_search"})
    assert after == before + 3


@pytest.mark.anyio
async def test_backend_robots_disallow_disables() -> None:
    clock = FakeClock()
    backend = FakeBackend(robots=(200, "User-agent: *\nDisallow: /html/\n"))
    gate = gate_with(clock, min_interval_s=0.0)
    job = SearchJob()
    assert await gate.search(backend, "a", language="de", country="DE", job=job) == []
    assert any("robots.txt disallows" in w for w in job.warnings) and backend.calls == 0
    clock.now += 10 * C.WEB_SEARCH_COOLDOWN_S                               # for the process lifetime
    assert await gate.search(backend, "b", language="de", country="DE", job=SearchJob()) == []
    assert backend.robots_calls == 1 and backend.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("robots", [(503, ""), (None, "")])
async def test_backend_robots_unreachable_disables_for_cooldown_only(robots) -> None:
    clock = FakeClock()
    backend = FakeBackend(robots=robots)
    gate = gate_with(clock, min_interval_s=0.0)
    job = SearchJob()
    assert await gate.search(backend, "a", language="de", country="DE", job=job) == []
    assert any("could not be read" in w for w in job.warnings)
    clock.now += C.WEB_SEARCH_COOLDOWN_S / 2
    assert await gate.search(backend, "b", language="de", country="DE", job=SearchJob()) == []
    assert backend.robots_calls == 1                                       # not re-fetched in the cooldown
    clock.now += C.WEB_SEARCH_COOLDOWN_S
    backend.robots = (404, "")
    assert len(await gate.search(backend, "c", language="de", country="DE", job=SearchJob())) == 1
    assert backend.robots_calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [httpx.Response(202, text=""), httpx.Response(429, text=""),
                                    httpx.Response(403, text="forbidden"),
                                    httpx.Response(200, text=fixture("ddg_anomaly.html"))])
async def test_breaker_on_anomaly_202_429_disables_for_rest_of_job(answer: httpx.Response, search_mock) -> None:
    clock = FakeClock()
    gate = gate_with(clock, min_interval_s=0.0)
    backend = DuckDuckGoHtml()
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get("https://html.duckduckgo.com/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
        route = mock.get(url__startswith=DDG).mock(return_value=answer)
        job = SearchJob()
        assert await gate.search(backend, "kranich", language="de", country="DE", job=job) == []
        assert await gate.search(backend, "kranich", language="de", country="DE", job=job) == []
        assert route.call_count == 1 and job.disabled and len(job.warnings) == 1
        new_job = SearchJob()                                              # inside the cooldown
        assert await gate.search(backend, "x", language="de", country="DE", job=new_job) == []
        assert route.call_count == 1 and new_job.warnings
        clock.now += C.WEB_SEARCH_COOLDOWN_S + 1
        route.mock(return_value=httpx.Response(200, text=fixture("ddg_results.html")))
        assert len(await gate.search(backend, "kranich", language="de", country="DE", job=SearchJob())) == 10


@pytest.mark.anyio
async def test_ddg_request_shape_and_honest_user_agent(search_mock) -> None:
    backend = build_search_backend(load_settings({}))
    with respx.mock(assert_all_mocked=True) as mock:
        search_mock.serp("kranich", [("https://kranich-transporte-example.de/", "Kranich Transporte")]).install(mock)
        results = await search_mock.gate.search(backend, '"Kranich Transporte" Bremen', language="de",
                                                country="DE", job=SearchJob())
        request = [c.request for c in mock.calls if c.request.url.path == "/html/"][0]
    assert results == [SearchResult("https://kranich-transporte-example.de", "Kranich Transporte", "Kranich Transporte")]
    assert request.url.params["kl"] == "de-de" and request.url.params["q"] == '"Kranich Transporte" Bremen'
    assert request.headers["user-agent"] == load_settings({}).crawler_user_agent


@pytest.mark.anyio
async def test_searxng_backend_via_search_mock(search_mock) -> None:
    backend = build_search_backend(load_settings({"WEB_SEARCH_URL": "http://searxng.test"}))
    with respx.mock(assert_all_mocked=True) as mock:
        search_mock.serp("kranich", [("https://kranich-transporte-example.de/", "Kranich")]).install(mock)
        results = await search_mock.gate.search(backend, "kranich bremen", language="de", country="DE",
                                                job=SearchJob())
        search_call = [c.request for c in mock.calls if c.request.url.path == "/search"][0]
    assert [r.url for r in results] == ["https://kranich-transporte-example.de"]
    assert search_call.url.params["format"] == "json" and search_call.url.params["language"] == "de"


# --- SSRF --------------------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_result_urls_ssrf_checked() -> None:
    from leadscraper.crawler.fetcher import Fetcher
    from leadscraper.crawler.website import HostGuard
    html = ('<div class="result"><a class="result__a" href="http://10.0.0.1/admin">x</a></div>'
            '<div class="result"><a class="result__a" href="https://intern-example.de/">y</a></div>')
    results = parse_ddg_html(html)
    assert [r.url for r in results] == ["https://intern-example.de"]       # IP literal never kept

    async def private(_host: str) -> list[str]:
        return ["10.0.0.7"]

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.get(url__regex=r"https?://.*").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            fetcher = Fetcher(load_settings({}), client, HostGuard(private))
            res = await fetcher.fetch(results[0].url)
    assert route.call_count == 0 and res.skipped == "non_public_address"


# --- the conftest guard ----------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_unmocked_search_request_fails_test() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="unmocked web search request"):
            await client.get("https://html.duckduckgo.com/html/?q=x")
    with httpx.Client() as client, pytest.raises(AssertionError, match="unmocked web search request"):
        client.get("https://www.google.com/search?q=x")


def test_search_guard_skipped_for_live_marker(search_guard_decision) -> None:
    class Node:
        def __init__(self, markers: set[str], fixtures: tuple[str, ...] = ()) -> None:
            self.markers, self.fixturenames = markers, fixtures

        def get_closest_marker(self, name: str):
            return name if name in self.markers else None

    assert search_guard_decision(Node(set())) is True
    assert search_guard_decision(Node({"live"})) is False
    assert search_guard_decision(Node({"benchmark"})) is False
    assert search_guard_decision(Node(set(), ("search_mock",))) is False


def test_no_outbound_call_at_startup(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from leadscraper.main import create_app
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs")}))
    with TestClient(app) as client:                                         # lifespan runs; guard active
        assert client.get("/health").status_code == 200
    assert isinstance(app.state.pipeline_deps.search_backend, DuckDuckGoHtml)
    assert app.state.pipeline_deps.search_gate is app.state.search_gate
    assert app.state.search_gate.requests == 0


@pytest.mark.anyio
async def test_budget_not_exceeded_under_concurrency() -> None:
    """callers queued on the gate lock must not overrun the job budget."""
    clock, backend = FakeClock(), FakeBackend()
    gate, job = gate_with(clock, min_interval_s=0.0), SearchJob(budget=3)
    results = await asyncio.gather(*(gate.search(backend, f"q{i}", language="de", country="DE", job=job)
                                     for i in range(10)))
    assert backend.calls == 3 and job.used == 3 and sum(1 for r in results if r) == 3
    assert job.warnings == ["Web search budget exhausted for this job; results may be fewer than max_output."]


@pytest.mark.anyio
async def test_breaker_stops_queued_requests() -> None:
    """Blocked on call 2 → no further request from the queued callers, one warning."""
    clock, backend = FakeClock(), FakeBackend(block=2)
    gate, job = gate_with(clock, min_interval_s=0.0), SearchJob()
    await asyncio.gather(*(gate.search(backend, f"q{i}", language="de", country="DE", job=job)
                           for i in range(10)))
    assert backend.calls == 2 and job.disabled and len(job.warnings) == 1
