from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from leadscraper.main import create_app
from leadscraper.settings import load_settings

GERMANY = {
    "country": "Germany",
    "regions": ["Bayern", "Nordrhein-Westfalen", "Hessen"],
    "industries": ["Maschinenbau", "Logistik", "Großhandel", "Produktion"],
    "information": ["company_name", "company_email", "website"],
    "max_output": 1000,
}
FRANCE = {
    "country": "France",
    "regions": ["Île-de-France", "Auvergne-Rhône-Alpes"],
    "industries": ["Transport routier", "Commerce de gros"],
    "information": ["company_name", "company_email", "website", "phone"],
    "max_output": 300,
}
UNITED_STATES = {
    "country": "United States",
    "regions": ["California", "Texas"],
    "industries": ["Manufacturing", "Logistics"],
    "information": ["company_name", "company_email", "website"],
    "max_output": 500,
}
RESOLVED_KEYS = {"country", "regions", "industries", "sources", "known_in_job", "compliance_note",
                 "warnings"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path)}))
    with respx.mock(assert_all_mocked=True) as mock:   # any outbound HTTP call fails the test
        with TestClient(app) as c:
            yield c
        assert mock.calls.call_count == 0


def resolve(client: TestClient, body: dict) -> dict:
    resp = client.post("/scrape/resolve", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "success" and set(data) == {"status", "resolved"}
    assert set(data["resolved"]) == RESOLVED_KEYS
    assert "known_in_db" not in data["resolved"]
    return data["resolved"]


def test_united_states_tier_c_block(client: TestClient) -> None:
    r = resolve(client, UNITED_STATES)
    assert r["country"] == {"input": "United States", "code": "US", "languages": ["en"], "tier": "C"}
    assert r["regions"] == [
        {"input": "California", "id": "iso:US-CA", "name": "California", "level": "state",
         "method": "exact"},
        {"input": "Texas", "id": "iso:US-TX", "name": "Texas", "level": "state",
         "method": "exact"},
    ]
    manufacturing, logistics = r["industries"]
    assert manufacturing == {"input": "Manufacturing", "isic": ["C"], "scheme": "ISIC",
                             "version": "Rev.4",
                             "keywords": {"en": ["manufacturing", "factory", "manufacturer", "works"]},
                             "method": "catalog"}
    assert logistics["isic"] == ["49", "52", "53"] and logistics["method"] == "catalog"
    assert logistics["keywords"] == {"en": ["logistics", "freight", "forwarding", "warehouse"]}
    assert r["sources"] == ["osm", "web_search"]
    assert r["known_in_job"] == 0
    assert r["compliance_note"] == ("US: Check local data protection and outreach rules before "
                                    "using the data for marketing.")
    assert r["warnings"] == ["Tier C: business websites are not required to publish a legal notice, "
                             "so the email yield is expected to be lower."]


def test_germany(client: TestClient) -> None:
    r = resolve(client, GERMANY)
    assert r["country"]["code"] == "DE" and r["country"]["tier"] == "B"
    assert [x["id"] for x in r["regions"]] == ["iso:DE-BY", "iso:DE-NW", "iso:DE-HE"]
    assert [x["isic"] for x in r["industries"]] == [["28"], ["49", "52", "53"], ["46"], ["C"]]
    assert [x["input"] for x in r["industries"]] == GERMANY["industries"]
    assert r["sources"] == ["osm", "web_search"] and r["warnings"] == []


def test_france_tier_a_register_note(client: TestClient) -> None:
    r = resolve(client, FRANCE)
    assert r["country"]["code"] == "FR" and r["country"]["tier"] == "A"
    assert [x["id"] for x in r["regions"]] == ["iso:FR-IDF", "iso:FR-ARA"]
    assert [x["isic"] for x in r["industries"]] == [["49"], ["46"]]
    assert r["sources"] == ["osm", "web_search"]
    assert any("fr_recherche_entreprises" in w for w in r["warnings"])


def test_empty_regions_means_whole_country(client: TestClient) -> None:
    r = resolve(client, {**GERMANY, "regions": []})
    assert r["regions"] == [{"input": None, "id": "iso:DE", "name": "Germany", "level": "country",
                             "method": "country"}]


def test_fuzzy_region_warns(client: TestClient) -> None:
    r = resolve(client, {**GERMANY, "regions": ["Nieder-Sachsen"]})
    assert r["regions"][0]["method"] == "fuzzy" and r["regions"][0]["id"] == "iso:DE-NI"
    assert any("Nieder-Sachsen" in w for w in r["warnings"])


def test_unavailable_information_warns(client: TestClient) -> None:
    r = resolve(client, {**GERMANY, "country": "GB", "regions": [],
                         "information": ["company_email", "register_number", "vat_id"]})
    assert any("register_number" in w for w in r["warnings"])
    assert not any("'vat_id'" in w for w in r["warnings"])      # stdnum gb.vat exists


def test_bayerm_exact_error_envelope(client: TestClient) -> None:
    resp = client.post("/scrape/resolve", json={**GERMANY, "regions": ["Bayerm"]})
    assert resp.status_code == 422
    assert resp.json() == {
        "status": "error",
        "error": {
            "code": "unresolved_region",
            "message": "Region 'Bayerm' could not be resolved for country DE",
            "details": {"input": "Bayerm", "suggestions": [{"id": "iso:DE-BY", "name": "Bayern"}]},
        },
    }


def test_nord_without_suggestions_has_hint(client: TestClient) -> None:
    body = client.post("/scrape/resolve", json={**GERMANY, "regions": ["Nord"]}).json()
    assert body["error"]["code"] == "unresolved_region"
    assert body["error"]["details"]["suggestions"] == []
    assert "NOMINATIM_URL" in body["error"]["details"]["hint"]


def test_unresolved_country(client: TestClient) -> None:
    body = client.post("/scrape/resolve", json={**GERMANY, "country": "Qwertzuiop"}).json()
    assert body["status"] == "error" and body["error"]["code"] == "unresolved_country"
    assert body["error"]["details"]["input"] == "Qwertzuiop"


def test_great_britain_alias_country(client: TestClient) -> None:
    r = resolve(client, {**GERMANY, "country": "Great Britain", "regions": []})
    assert r["country"]["code"] == "GB"


def test_unresolved_industry(client: TestClient) -> None:
    resp = client.post("/scrape/resolve", json={**GERMANY, "industries": ["Blorptastic Wibble"]})
    body = resp.json()
    assert resp.status_code == 422 and body["error"]["code"] == "unresolved_industry"
    sugg = body["error"]["details"]["suggestions"]
    assert 1 <= len(sugg) <= 3 and all(set(s) == {"isic", "title"} for s in sugg)


def test_validation_error_still_envelope(client: TestClient) -> None:
    resp = client.post("/scrape/resolve", json={"country": "DE", "information": []})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


def test_industries_optional_resolves_to_all_companies(client: TestClient) -> None:
    r = resolve(client, {"country": "DE", "regions": ["Bremen"],
                         "information": ["company_name", "company_email", "website"]})
    assert r["industries"] == [{"input": "", "isic": [], "scheme": "ISIC", "version": "Rev.4",
                                "keywords": {}, "method": "any"}]
    assert any("No industries given" in w for w in r["warnings"])


def test_meta_countries(client: TestClient) -> None:
    body = client.get("/meta/countries").json()
    assert body["status"] == "success" and body["count"] > 240
    assert {"code": "DE", "alpha_3": "DEU", "name": "Germany"} in body["countries"]


def test_meta_regions(client: TestClient) -> None:
    body = client.get("/meta/regions", params={"country": "FR"}).json()
    assert body["country"] == "FR"
    assert any(r["id"] == "iso:FR-IDF" and r["name"] == "Île-de-France" for r in body["regions"])
    assert client.get("/meta/regions", params={"country": "France"}).json()["country"] == "FR"
    assert client.get("/meta/regions", params={"country": "Qwertzuiop"}).status_code == 422
    assert client.get("/meta/regions").status_code == 422


def test_meta_industries(client: TestClient) -> None:
    body = client.get("/meta/industries", params={"q": "logistik"}).json()
    assert body["status"] == "success" and body["industries"]
    assert body["industries"][0]["isic"] == ["49", "52", "53"]
    assert (body["scheme"], body["version"]) == ("ISIC", "Rev.4")


def test_nominatim_used_only_when_configured(tmp_path: Path) -> None:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path),
                                    "NOMINATIM_URL": "https://nominatim.example"}))
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://nominatim.example/search").mock(return_value=httpx.Response(
            200, json=[{"osm_type": "relation", "osm_id": 62428, "name": "München",
                        "addresstype": "city", "boundingbox": ["48.06", "48.25", "11.36", "11.72"]}]))
        with TestClient(app) as c:
            r = c.post("/scrape/resolve", json={**GERMANY, "regions": ["Munchen", "Bayern"]}).json()
    assert route.call_count == 1                       # only the unresolvable region hit Nominatim
    assert r["resolved"]["regions"][0] == {"input": "Munchen", "id": "osm:r62428", "name": "München",
                                           "level": "city", "method": "nominatim"}
