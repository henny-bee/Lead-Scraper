import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from leadscraper.domain.models import SearchSlice
from leadscraper.observability import metrics
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.industry import default_catalog
from leadscraper.services.resolver.profile import ProfileBuilder
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import (
    DEFAULT_OVERPASS_URL,
    OverpassAdapter,
    OverpassGate,
    OverpassGatePool,
    SourceBudget,
    overpass_endpoints,
)
from leadscraper.sources.registry import build_adapters, enabled_source_names

pytestmark = pytest.mark.anyio

URL = "https://overpass.test/api/interpreter"
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "overpass"
NRW = geo.resolve_region_detailed("DE", "Nordrhein-Westfalen").area
HESSEN = geo.resolve_region_detailed("DE", "Hessen").area
MANUFACTURING = default_catalog().resolve("Produktion", ("de",)).profile


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def make_slice(area=NRW, quota: int = 50) -> SearchSlice:
    return SearchSlice(country_code="DE", area_id=area.id, industry_profile_id=MANUFACTURING.id,
                       region_label=area.input or area.name, industry_label="Produktion", quota=quota)


def fast_gate(**kw) -> OverpassGate:
    return OverpassGate(per_minute=600_000, **kw)       # ~0.1 ms spacing in tests


def make_adapter(client: httpx.AsyncClient, *, budget: int = 100, gate: OverpassGate | None = None,
                 attempts: int = 3) -> OverpassAdapter:
    return OverpassAdapter(
        overpass_url=URL, user_agent="LeadScraperBot/test", gate=gate or fast_gate(),
        budget=SourceBudget("osm", budget), areas={NRW.id: NRW, HESSEN.id: HESSEN},
        industries={MANUFACTURING.id: MANUFACTURING}, languages=("de",), client=client,
        retry_attempts=attempts, retry_wait_s=0.001, retry_wait_max_s=0.002)


def budget_metric() -> float:
    return metrics.REGISTRY.get_sample_value("source_budget_used", {"source": "osm"}) or 0.0


async def collect(adapter: OverpassAdapter, slice_: SearchSlice) -> list:
    return [c async for c in adapter.discover(slice_)]


async def test_candidates_from_mocked_response() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json=fixture("nrw_manufacturing.json")))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            s = make_slice()
            cands = await collect(adapter, s)
    assert [c.source_ref for c in cands] == ["node/1001", "way/2002", "relation/3003"]
    first, second, third = cands
    assert first.name == "Muster Maschinenbau GmbH" and first.website == "https://muster-maschinenbau-example.de"
    assert first.hints == {"email": "info@muster-maschinenbau-example.de", "city": "Düsseldorf"}
    assert first.postal_code == "40210" and first.coords_storable and first.lat == 51.2277
    assert second.website == "beispiel-textil-example.de" and second.hints["email"] == "kontakt@beispiel-textil-example.de"
    assert second.hints["phone"] == "+49 201 123456" and (second.lat, second.lon) == (51.4556, 7.0116)
    assert third.website is None                      # skipped later by the website stage
    outcome = adapter.outcomes[s]
    assert outcome.exhausted and outcome.reason == "done" and outcome.candidates == 3
    req = route.calls[0].request
    assert req.headers["user-agent"] == "LeadScraperBot/test"
    query = parse_qs(req.content.decode())["data"][0]
    assert 'area["ISO3166-2"="DE-NW"]' in query and "out tags center 5000;" in query
    assert "api_key" not in str(req.url).lower() and "authorization" not in req.headers


async def test_budget_metric_increments_and_adapter_stops_at_budget() -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json=fixture("nrw_manufacturing.json")))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, budget=2)
            before = budget_metric()
            await collect(adapter, make_slice(quota=10))
            assert budget_metric() == before + 1
            await collect(adapter, make_slice(HESSEN, quota=10))
            assert budget_metric() == before + 2 and adapter.budget.exhausted
            third = make_slice(quota=11)
            assert await collect(adapter, third) == []
    assert route.call_count == 2                      # no request beyond the budget
    assert budget_metric() == before + 2
    assert adapter.outcomes[third].exhausted and adapter.outcomes[third].reason == "budget"
    assert any("Overpass request budget exhausted" in w for w in adapter.warnings)


async def test_429_retried_then_success() -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(side_effect=[
            httpx.Response(429), httpx.Response(504),
            httpx.Response(200, json=fixture("nrw_manufacturing.json"))])
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            cands = await collect(adapter, make_slice())
    assert route.call_count == 3 and len(cands) == 3
    assert adapter.budget.used == 3                   # every attempt counts against the budget


async def test_429_exhaustion_is_graceful() -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(429))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, attempts=3)
            s = make_slice()
            assert await collect(adapter, s) == []    # no exception -> job continues
    assert route.call_count == 3
    assert adapter.outcomes[s].exhausted and adapter.outcomes[s].reason == "error"
    assert adapter.warnings and "slice skipped" in adapter.warnings[0]


async def test_timeout_exception_retried_then_exhausted() -> None:
    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, attempts=2)
            s = make_slice()
            assert await collect(adapter, s) == []
    assert adapter.outcomes[s].reason == "error" and adapter.budget.used == 2


async def test_overpass_runtime_error_marks_slice_exhausted() -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json=fixture("runtime_error.json")))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            s = make_slice()
            assert await collect(adapter, s) == []
    assert route.call_count == 1                      # not retried, no sweep() in v0.3
    assert adapter.outcomes[s].reason == "runtime_error"
    assert any("timed out" in w for w in adapter.warnings)


async def test_non_retryable_error_and_bad_json() -> None:
    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=[httpx.Response(400, text="bad query"),
                                         httpx.Response(200, text="<html>not json</html>")])
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            assert await collect(adapter, make_slice()) == []
            assert await collect(adapter, make_slice(HESSEN)) == []
    assert adapter.budget.used == 2 and len(adapter.outcomes) == 2


async def test_at_most_max_concurrency_in_flight() -> None:
    """≤ ``OVERPASS_MAX_CONCURRENCY`` (2) requests per endpoint in flight."""
    from leadscraper import constants as C
    gate = fast_gate()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"elements": []})

    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=slow)
        async with httpx.AsyncClient() as client:
            adapters = [make_adapter(client, gate=gate) for _ in range(3)]  # e.g. three jobs
            await asyncio.gather(*(collect(a, make_slice(quota=q)) for q, a in enumerate(adapters, 1)))
    assert gate.max_in_flight <= C.OVERPASS_MAX_CONCURRENCY


async def test_rate_limiter_spaces_requests() -> None:
    gate = OverpassGate(per_minute=600)                # one request per 100 ms
    stamps: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        stamps.append(asyncio.get_running_loop().time())
        return httpx.Response(200, json={"elements": []})

    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=record)
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, gate=gate)
            for q in (1, 2, 3):
                await collect(adapter, make_slice(quota=q))
    assert len(stamps) == 3
    assert min(b - a for a, b in zip(stamps, stamps[1:])) >= 0.08


async def test_daily_budget_in_process() -> None:
    gate = fast_gate(daily_budget=1)
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json={"elements": []}))
        async with httpx.AsyncClient() as client:
            a = make_adapter(client, gate=gate)
            await collect(a, make_slice(quota=1))
            s = make_slice(quota=2)
            await collect(a, s)
    assert route.call_count == 1 and a.outcomes[s].reason == "daily_budget"
    assert a.daily_budget == 1


async def test_registry_only_osm() -> None:
    settings = load_settings({"GOOGLE_PLACES_ENABLED": "true", "GOOGLE_PLACES_API_KEY": "x",
                              "WEB_SEARCH_URL": "off"})
    assert enabled_source_names(settings) == ("osm",)
    profile = ProfileBuilder().get("DE")
    adapters = build_adapters(settings, profile, gate=fast_gate(), areas={NRW.id: NRW},
                              industries={MANUFACTURING.id: MANUFACTURING})
    assert [a.name for a in adapters] == ["osm"]
    osm = adapters[0]
    assert osm.overpass_url == "https://overpass-api.de/api/interpreter"
    assert osm.budget.limit == 100 and osm.languages == ("de",)
    assert osm.countries is None


async def test_registry_default_adds_web_search() -> None:
    """Sibling of ``test_registry_only_osm``: by default (search on) the registry also enables
    ``web_search``; its adapter is built only with a search backend and gate."""
    from leadscraper.sources.web_search import DuckDuckGoHtml, SearchGate, WebSearchAdapter
    settings = load_settings({"GOOGLE_PLACES_ENABLED": "true", "GOOGLE_PLACES_API_KEY": "x"})
    assert enabled_source_names(settings) == ("osm", "web_search")
    profile = ProfileBuilder(enabled_sources=enabled_source_names(settings)).get("DE")
    assert profile.sources == ["osm", "web_search"]
    kwargs = dict(gate=fast_gate(), areas={NRW.id: NRW}, industries={MANUFACTURING.id: MANUFACTURING})
    assert [a.name for a in build_adapters(settings, profile, **kwargs)] == ["osm"]   # no backend
    adapters = build_adapters(settings, profile, **kwargs, search_backend=DuckDuckGoHtml(),
                              search_gate=SearchGate())
    assert [a.name for a in adapters] == ["osm", "web_search"]
    web = adapters[1]
    assert isinstance(web, WebSearchAdapter) and web.country == "DE" and web.languages == ("de",)
    assert web.budget == 20 and web.countries is None


async def test_output_cap_is_fixed_and_independent_of_quota() -> None:
    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json={"elements": []}))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            for quota in (1, 50, 4999):
                await collect(adapter, make_slice(quota=quota))
    queries = [parse_qs(c.request.content.decode())["data"][0] for c in route.calls]
    assert len(queries) == 3
    assert all(q.rstrip().endswith("out tags center 5000;") for q in queries)
    assert all(o.reason == "done" for o in adapter.outcomes.values())


async def test_exactly_cap_elements_is_saturated() -> None:
    elements = [{"type": "node", "id": i, "lat": 51.2, "lon": 6.8,
                 "tags": {"name": f"Fabrik {i}", "website": f"https://f{i}.de"}} for i in range(3)]
    before = metrics.REGISTRY.get_sample_value("tiles_saturated_total", {"source": "osm"}) or 0.0
    with respx.mock() as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json={"elements": elements}))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            adapter.max_elements = 3
            s = make_slice(quota=1)
            cands = await collect(adapter, s)
            assert "out tags center 3;" in parse_qs(
                mock.calls[0].request.content.decode())["data"][0]
    assert len(cands) == 3                             # truncated results are still used
    assert adapter.outcomes[s].exhausted and adapter.outcomes[s].reason == "saturated"
    after = metrics.REGISTRY.get_sample_value("tiles_saturated_total", {"source": "osm"})
    assert after == before + 1
    assert any("truncated at 3 elements" in w for w in adapter.warnings)


async def test_error_warning_names_the_cause() -> None:
    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=[httpx.Response(400, text="parse error"),
                                         httpx.Response(504)])
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, attempts=1)
            await collect(adapter, make_slice())
            await collect(adapter, make_slice(HESSEN))
    assert any("HTTPStatusError 400" in w for w in adapter.warnings)
    assert any("http 504" in w for w in adapter.warnings)


async def test_overpass_request_uses_long_timeout_even_on_shared_short_client() -> None:
    """The pipeline shares the crawler client (20 s); Overpass needs > [timeout:180]."""
    from leadscraper import constants as C

    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json={"elements": []}))
        async with httpx.AsyncClient(timeout=C.CRAWLER_HTTP_TIMEOUT_S) as client:
            await collect(make_adapter(client), make_slice())
    timeout = route.calls[0].request.extensions["timeout"]
    assert timeout["read"] == C.OVERPASS_HTTP_TIMEOUT_S > C.OVERPASS_QUERY_TIMEOUT_S


async def test_osm_elements_ordered_website_first() -> None:
    """Elements are stably sorted by tier (website → email only → neither) before mapping."""
    def el(i: int, **tags: str) -> dict:
        return {"type": "node", "id": i, "lat": 51.0, "lon": 7.0, "tags": {"name": f"Maschinenbau {i}", **tags}}

    elements = [el(1), el(2, email="a@two-example.de"), el(3, website="https://three-example.de"),
                el(4), el(5, **{"contact:website": "five-example.de"}), el(6, **{"contact:email": "b@six-example.de"})]
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json={"elements": elements}))
        async with httpx.AsyncClient() as client:
            cands = await collect(make_adapter(client), make_slice())
    assert [c.source_ref for c in cands] == ["node/3", "node/5", "node/2", "node/6", "node/1", "node/4"]


# --- gate pool, 2 slots per endpoint, bounded mirror failover ----------------------------------------
MIRROR_1 = "https://mirror-one.test/api/interpreter"
MIRROR_2 = "https://mirror-two.test/api/interpreter"


def pool_adapter(client: httpx.AsyncClient, endpoints=(URL, MIRROR_1, MIRROR_2), **kw) -> OverpassAdapter:
    return make_adapter(client, gate=OverpassGatePool(endpoints, per_minute=600_000), **kw)


def remark(n: int) -> dict:
    return {"remark": "runtime error: Query timed out in \"query\" at line 4 after 181 seconds.",
            "elements": [
                {"type": "node", "id": 7000 + i, "lat": 51.0, "lon": 7.0,
                 "tags": {"name": f"Maschinenbau Teil {i}"}} for i in range(n)]}


def test_endpoints_mirrors_only_for_the_public_default() -> None:
    from leadscraper import constants as C
    assert overpass_endpoints(DEFAULT_OVERPASS_URL) == (DEFAULT_OVERPASS_URL, *C.OVERPASS_MIRROR_URLS)
    assert overpass_endpoints("https://overpass.intern.test/api/interpreter") == (
        "https://overpass.intern.test/api/interpreter",)
    default_pool = OverpassGatePool.for_settings(load_settings({}))
    assert default_pool.endpoints[0] == DEFAULT_OVERPASS_URL and len(default_pool.endpoints) == 3
    assert default_pool.concurrency == 3 * C.OVERPASS_MAX_CONCURRENCY
    custom = OverpassGatePool.for_settings(load_settings({"OVERPASS_URL": URL}), per_minute=600_000)
    assert custom.endpoints == (URL,) and custom.concurrency == C.OVERPASS_MAX_CONCURRENCY


async def test_plain_gate_is_a_single_endpoint_pool() -> None:
    gate = fast_gate()
    with respx.mock(assert_all_mocked=True):
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, gate=gate)
    assert adapter.endpoints == (URL,) and adapter.pool.gate(URL) is gate


async def test_failover_to_mirror_on_429() -> None:
    stamps: list[float] = []

    def at(response: httpx.Response):
        def record(_request: httpx.Request) -> httpx.Response:
            stamps.append(asyncio.get_running_loop().time())
            return response
        return record

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        primary = mock.post(URL).mock(side_effect=at(httpx.Response(429)))
        mirror = mock.post(MIRROR_1).mock(side_effect=at(httpx.Response(
            200, json=fixture("nrw_manufacturing.json"))))
        other = mock.post(MIRROR_2).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            adapter = pool_adapter(client)
            adapter.retry_wait_s = 1.0                 # a backoff would be ≥ 1 s
            s = make_slice()
            cands = await collect(adapter, s)
    assert primary.call_count == 1 and mirror.call_count == 1 and other.call_count == 0
    assert len(cands) == 3 and adapter.outcomes[s].reason == "done" and adapter.budget.used == 2
    assert stamps[1] - stamps[0] < adapter.retry_wait_s
    mirror_timeout = mirror.calls[0].request.extensions["timeout"]
    from leadscraper import constants as C
    assert mirror_timeout["connect"] == C.OVERPASS_MIRROR_CONNECT_TIMEOUT_S
    assert mirror_timeout["read"] == C.OVERPASS_HTTP_TIMEOUT_S


async def test_failover_total_attempts_bounded() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        routes = [mock.post(u).mock(return_value=httpx.Response(504)) for u in (URL, MIRROR_1, MIRROR_2)]
        async with httpx.AsyncClient() as client:
            adapter = pool_adapter(client)
            s = make_slice()
            assert await collect(adapter, s) == []
    assert [r.call_count for r in routes] == [1, 1, 1]        # exactly 3 POSTs, one per endpoint
    assert adapter.outcomes[s].reason == "error" and adapter.budget.used == 3
    assert any("slice skipped" in w and "http 504 at mirror-two.test" in w for w in adapter.warnings)


async def test_runtime_error_remark_fails_over() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json=remark(1)))
        mirror = mock.post(MIRROR_1).mock(return_value=httpx.Response(200, json=fixture("nrw_manufacturing.json")))
        async with httpx.AsyncClient() as client:
            adapter = pool_adapter(client)
            s = make_slice()
            cands = await collect(adapter, s)
    assert mirror.call_count == 1 and len(cands) == 3
    assert adapter.outcomes[s].reason == "done" and not adapter.warnings


async def test_runtime_error_partial_kept_when_failover_fails() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        for url, n in ((URL, 2), (MIRROR_1, 5), (MIRROR_2, 3)):
            mock.post(url).mock(return_value=httpx.Response(200, json=remark(n)))
        async with httpx.AsyncClient() as client:
            adapter = pool_adapter(client)
            s = make_slice()
            cands = await collect(adapter, s)
    assert len(cands) == 5                                     # the best attempt's partial elements
    assert adapter.outcomes[s].reason == "runtime_error" and adapter.budget.used == 3
    assert any("timed out" in w and "mirror-one.test" in w for w in adapter.warnings)


async def test_network_error_fails_over_but_single_endpoint_is_unchanged() -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        mock.post(MIRROR_1).mock(return_value=httpx.Response(200, json=fixture("nrw_manufacturing.json")))
        async with httpx.AsyncClient() as client:
            adapter = pool_adapter(client)
            assert len(await collect(adapter, make_slice())) == 3
    with respx.mock(assert_all_mocked=True) as mock:
        only = mock.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as client:
            single = make_adapter(client)
            s = make_slice()
            assert await collect(single, s) == []
    assert only.call_count == 1 and single.outcomes[s].reason == "error"      # no retry, as in v0.3
    assert any("(ConnectError at overpass.test)" in w and "slice skipped" in w for w in single.warnings)


async def test_runtime_error_partial_kept_when_last_attempt_connect_error() -> None:
    """runtime-error partial (4) → 504 → ConnectError keeps the partial."""
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json=remark(4)))
        mock.post(MIRROR_1).mock(return_value=httpx.Response(504))
        mock.post(MIRROR_2).mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as client:
            adapter = pool_adapter(client)
            s = make_slice()
            cands = await collect(adapter, s)
    assert len(cands) == 4 and adapter.outcomes[s].reason == "runtime_error"


async def test_runtime_error_partial_kept_when_budget_runs_out() -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(URL).mock(return_value=httpx.Response(200, json=remark(3)))
        mirror = mock.post(MIRROR_1).mock(return_value=httpx.Response(200, json=remark(9)))
        async with httpx.AsyncClient() as client:
            adapter = pool_adapter(client, budget=1)
            s = make_slice()
            cands = await collect(adapter, s)
    assert mirror.call_count == 0 and adapter.budget.used == 1
    assert len(cands) == 3 and adapter.outcomes[s].reason == "runtime_error"


async def test_custom_overpass_url_never_uses_mirrors() -> None:
    from leadscraper import constants as C
    settings = load_settings({"OVERPASS_URL": URL})
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(429))
        public = [mock.post(u).mock(return_value=httpx.Response(200, json={"elements": []}))
                  for u in (DEFAULT_OVERPASS_URL, *C.OVERPASS_MIRROR_URLS)]
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, gate=OverpassGatePool.for_settings(settings, per_minute=600_000))
            s = make_slice()
            assert await collect(adapter, s) == []
    assert route.call_count == 3 and all(r.call_count == 0 for r in public)
    assert adapter.endpoints == (URL,) and adapter.outcomes[s].reason == "error"


# --- area gazetteer ---------------------------------------------------------------------------------
GAZETTEER = {"elements": [
    {"type": "relation", "id": 1, "tags": {"boundary": "postal_code", "postal_code": "40210"}},
    {"type": "node", "id": 2, "tags": {"place": "city", "name": "Düsseldorf"}}]}


def gazetteer_queries(route) -> list[str]:
    return [q for c in route.calls if '"boundary"="postal_code"' in
            (q := parse_qs(c.request.content.decode())["data"][0])]


async def test_gazetteer_queried_once_per_area_and_counts_toward_budget() -> None:
    before = budget_metric()
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json=GAZETTEER))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            first, second = await asyncio.gather(adapter.gazetteer(NRW), adapter.gazetteer(NRW))
            third = await adapter.gazetteer(NRW)
        queries = gazetteer_queries(route)
    assert first is second is third and first.postcodes == {"40210"} and first.places == {"duesseldorf"}
    assert len(queries) == 1 and 'area["ISO3166-2"="DE-NW"]' in queries[0] and "out tags;" in queries[0]
    assert adapter.budget.used == 1 and budget_metric() == before + 1 and not adapter.warnings


async def test_no_gazetteer_for_country_level_area() -> None:
    germany = geo.GeoArea(id="iso:DE", country_code="DE", name="Germany", level="country")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json=GAZETTEER))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client)
            assert await adapter.gazetteer(germany) is None
    assert route.call_count == 0 and adapter.budget.used == 0 and not adapter.warnings


async def test_gb_eng_split_area_gets_gazetteer() -> None:
    """Is the exact ``level == "country"``: the GB split area England (level "Country") gets its
    gazetteer."""
    from leadscraper.services.planner import country_split
    uk = geo.GeoArea(id="iso:GB", country_code="GB", name="United Kingdom", level="country")
    england = next(a for a in country_split(uk) if a.code == "GB-ENG")
    assert england.level == "Country"
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json={"elements": [
            {"type": "node", "id": 1, "tags": {"place": "city", "name": "Leeds"}}]}))
        async with httpx.AsyncClient() as client:
            gaz = await make_adapter(client).gazetteer(england)
        queries = gazetteer_queries(route)
    assert gaz is not None and gaz.places == {"leeds"}
    assert len(queries) == 1 and 'area["ISO3166-2"="GB-ENG"]' in queries[0]


async def test_gazetteer_failure_falls_back_and_warns_once() -> None:
    """A runtime-error remark (NRW) and an HTTP error (Hessen): both give ``None`` (the caller falls
    back to OSM postcodes and area names), one warning per job, and a failed area is not
    re-queried."""
    def answer(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["data"][0]
        if '"DE-NW"' in query:
            return httpx.Response(200, json={"elements": [], "remark": "runtime error: Query timed out"})
        return httpx.Response(400, text="bad request")

    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(URL).mock(side_effect=answer)
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, attempts=1)
            assert await adapter.gazetteer(NRW) is None
            assert await adapter.gazetteer(HESSEN) is None
            assert await adapter.gazetteer(NRW) is None                  # cached failure
        calls = route.call_count
    assert calls == 2 and len(adapter.warnings) == 1
    assert adapter.warnings[0].startswith("Overpass area gazetteer unavailable (runtime error at overpass.test)")


async def test_gazetteer_budget_exhausted_sends_nothing() -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json=GAZETTEER))
        async with httpx.AsyncClient() as client:
            adapter = make_adapter(client, budget=0)
            assert await adapter.gazetteer(NRW) is None
    assert route.call_count == 0 and len(adapter.warnings) == 1 and "request budget" in adapter.warnings[0]
