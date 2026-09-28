from pathlib import Path

import httpx
import pycountry
import pytest
import respx
import yaml

from leadscraper.services.resolver import geo
from leadscraper.services.resolver.geo import (
    Aliases,
    NominatimClient,
    load_aliases,
    norm,
    resolve_country_detailed,
    resolve_region_detailed,
)

CASES = yaml.safe_load((Path(__file__).resolve().parents[1] / "fixtures" / "resolver_cases.yaml")
                       .read_text(encoding="utf-8"))
NO_ALIASES = Aliases()


def _id(case: dict) -> str:
    suffix = ":noalias" if case.get("aliases") is False else ""
    return f"{case.get('country', '')}:{case['input']}{suffix}"


@pytest.mark.parametrize("case", CASES["countries"], ids=_id)
def test_country_golden(case: dict) -> None:
    aliases = NO_ALIASES if case.get("aliases") is False else None
    res = resolve_country_detailed(case["input"], aliases)
    assert res.code == case["expect"]
    if "method" in case:
        assert res.method == case["method"]
    if case["expect"] is None:
        assert res.method is None
        if "first_suggestion" in case:
            assert res.suggestions and res.suggestions[0] == case["first_suggestion"]
        if "suggestions" in case:
            assert list(res.suggestions) == case["suggestions"]


@pytest.mark.parametrize("case", CASES["regions"], ids=_id)
def test_region_golden(case: dict) -> None:
    aliases = NO_ALIASES if case.get("aliases") is False else None
    res = resolve_region_detailed(case["country"], case["input"], aliases)
    code = res.area.code if res.area else None
    assert code == case["expect"]
    if case["expect"] is not None:
        assert res.area.id == f"iso:{case['expect']}"
        assert res.area.input == case["input"]          # label kept for echo
        if "method" in case:
            assert res.method == case["method"]
    elif "suggestions" in case:
        assert list(res.suggestions) == case["suggestions"]


def test_norm() -> None:
    assert norm("Île-de-France") == "ile de france"
    assert norm("  Großhandel ") == "grosshandel"
    assert norm("Nordrhein_Westfalen") == "nordrhein westfalen"


def test_region_names_levels_and_methods() -> None:
    nrw = resolve_region_detailed("DE", "NRW").area
    assert (nrw.id, nrw.name, nrw.level, nrw.method) == (
        "iso:DE-NW", "Nordrhein-Westfalen", "land", "alias")
    ain = resolve_region_detailed("FR", "Ain").area
    assert (ain.name, ain.level) == ("Ain", "metropolitan department")


def test_suggestion_shape() -> None:
    assert geo.region_suggestion("DE-BY") == {"id": "iso:DE-BY", "name": "Bayern"}


def test_aliases_file_codes_are_valid() -> None:
    aliases = load_aliases()
    assert aliases.countries["great britain"] == "GB"
    for code in aliases.countries.values():
        assert pycountry.countries.get(alpha_2=code) is not None, code
    for cc, entries in aliases.regions.items():
        for key, code in entries.items():
            assert key == norm(key)
            sub = pycountry.subdivisions.get(code=code)
            assert sub is not None and sub.country_code == cc, code


def test_load_aliases_normalises_and_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "a.yaml"
    f.write_text("countries: {Great-Britain: gb}\nregions: {de: {N-R-W: de-nw}}\n", encoding="utf-8")
    a = load_aliases(f)
    assert a.countries == {"great britain": "GB"} and a.regions == {"DE": {"n r w": "DE-NW"}}
    assert load_aliases(tmp_path / "missing.yaml") == Aliases()


def test_warm_up_builds_large_index() -> None:
    assert geo.warm_up() > 30_000                      # ~36k CLDR names (A§3.2)


def test_list_helpers() -> None:
    regions = geo.list_regions("FR")
    assert {"id": "iso:FR-01", "code": "FR-01", "name": "Ain", "level": "metropolitan department",
            "parent": "iso:FR-ARA"} in regions
    assert any(c["code"] == "DE" for c in geo.list_countries())
    assert geo.country_area("DE").id == "iso:DE"


NOMINATIM_OK = [
    {"osm_type": "node", "osm_id": 1, "name": "München"},
    {"osm_type": "relation", "osm_id": 62428, "name": "München", "addresstype": "city",
     "boundingbox": ["48.06", "48.25", "11.36", "11.72"]},
]


@pytest.mark.anyio
async def test_nominatim_fallback_accepts_close_relation_and_caches() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://nominatim.local/search").mock(
            return_value=httpx.Response(200, json=NOMINATIM_OK))
        async with httpx.AsyncClient() as http:
            client = NominatimClient("https://nominatim.local/", max_rps=1, user_agent="Bot/1",
                                     client=http)
            area = await client.lookup("DE", "Munchen")
            again = await client.lookup("de", "munchen")
    assert area is again and route.call_count == 1       # cached, one request
    assert area.id == "osm:r62428" and area.osm_relation_id == 62428
    assert area.method == "nominatim" and area.bbox == (48.06, 11.36, 48.25, 11.72)
    req = route.calls[0].request
    assert req.headers["user-agent"] == "Bot/1" and req.url.params["countrycodes"] == "de"


@pytest.mark.anyio
async def test_nominatim_rejects_dissimilar_and_errors() -> None:
    with respx.mock() as mock:
        mock.get("https://n.local/search", params={"q": "Nord"}).mock(
            return_value=httpx.Response(200, json=[{"osm_type": "relation", "osm_id": 5,
                                                    "name": "Nordfriesland"}]))
        mock.get("https://n.local/search", params={"q": "Boom"}).mock(
            return_value=httpx.Response(503))
        async with httpx.AsyncClient() as http:
            client = NominatimClient("https://n.local", max_rps=5, user_agent="Bot", client=http,
                                     cache_size=1)
            assert await client.lookup("DE", "Nord") is None
            assert await client.lookup("DE", "Boom") is None
            assert len(client._cache) == 1                # bounded LRU


def test_no_network_during_resolution() -> None:
    with respx.mock(assert_all_mocked=True):             # any HTTP call would raise
        assert resolve_country_detailed("Alemania").code == "DE"
        assert resolve_region_detailed("DE", "Bayerm").area is None
