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
from leadscraper.sources.osm_overpass import OverpassAdapter, OverpassGate, SourceBudget
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


async def test_at_most_one_request_in_flight() -> None:
    gate = fast_gate()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"elements": []})

    with respx.mock() as mock:
        mock.post(URL).mock(side_effect=slow)
        async with httpx.AsyncClient() as client:
            adapters = [make_adapter(client, gate=gate) for _ in range(3)]  # e.g. three jobs
            await asyncio.gather(*(collect(a, make_slice(quota=q)) for q, a in enumerate(adapters, 1)))
    assert gate.max_in_flight == 1


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
    settings = load_settings({"GOOGLE_PLACES_ENABLED": "true", "GOOGLE_PLACES_API_KEY": "x"})
    assert enabled_source_names(settings) == ("osm",)
    profile = ProfileBuilder().get("DE")
    adapters = build_adapters(settings, profile, gate=fast_gate(), areas={NRW.id: NRW},
                              industries={MANUFACTURING.id: MANUFACTURING})
    assert [a.name for a in adapters] == ["osm"]
    osm = adapters[0]
    assert osm.overpass_url == "https://overpass-api.de/api/interpreter"
    assert osm.budget.limit == 100 and osm.languages == ("de",)
    assert osm.countries is None


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
    """The pipeline shares the crawler client (20 s); Overpass needs > [timeout:180] (T25)."""
    from leadscraper import constants as C

    with respx.mock() as mock:
        route = mock.post(URL).mock(return_value=httpx.Response(200, json={"elements": []}))
        async with httpx.AsyncClient(timeout=C.CRAWLER_HTTP_TIMEOUT_S) as client:
            await collect(make_adapter(client), make_slice())
    timeout = route.calls[0].request.extensions["timeout"]
    assert timeout["read"] == C.OVERPASS_HTTP_TIMEOUT_S > C.OVERPASS_QUERY_TIMEOUT_S
