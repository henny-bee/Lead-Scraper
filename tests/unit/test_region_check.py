"""Source-agnostic region check — pure rules."""

from leadscraper.domain.models import CompanyCandidate, GeoArea
from leadscraper.extractors.jsonld import JsonLdOrg
from leadscraper.extractors.text import page_text
from leadscraper.services.region_check import (
    AreaGazetteer,
    Evidence,
    company_evidence,
    is_city_level,
    needs_evidence,
    normalise_place,
    normalise_postcode,
    region_confidence,
)
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.profile import ProfileBuilder
from leadscraper.sources.osm_overpass import build_gazetteer_query, parse_gazetteer

PATTERNS = ProfileBuilder().get("DE").postal_patterns
BREMEN = geo.resolve_region_detailed("DE", "Bremen").area                 # state, not city-level
CITY = GeoArea(id="osm:r62559", country_code="DE", name="Düsseldorf", level="city",
               osm_relation_id=62559)
COUNTRY = GeoArea(id="iso:DE", country_code="DE", name="Germany", level="country")


def cand(source: str = "web_search", **hints: str) -> CompanyCandidate:
    return CompanyCandidate(name="Kranich Transporte", source=source, source_ref="x/1", hints=hints)


def check(evidence, gazetteer=None, osm_postcodes=(), area=BREMEN, candidate=None) -> str:
    return region_confidence(candidate or cand(), area=area, evidence=evidence, gazetteer=gazetteer,
                             osm_postcodes=osm_postcodes)


# --- rules 1–2 -----------------------------------------------------------------------------------
def test_osm_candidate_is_high_without_evidence() -> None:
    assert check(None, candidate=cand("osm")) == "high"
    assert not needs_evidence(cand("osm"))


def test_adapter_area_match_is_high() -> None:
    assert check(None, candidate=cand("fake_register", area_match="source")) == "high"
    assert not needs_evidence(cand("fake_register", area_match="source"))
    assert needs_evidence(cand("fake_register", area_match="bbox"))        # only "source" counts


def test_non_osm_without_evidence_is_low() -> None:
    assert check(None, osm_postcodes={"28195"}) == "low"
    assert check(Evidence(None, None), osm_postcodes={"28195"}) == "low"
    assert region_confidence(cand(), area=None, evidence=Evidence("28195", "Bremen"), gazetteer=None,
                             osm_postcodes={"28195"}) == "low"


# --- rule 3 --------------------------------------------------------------------------------------
def test_in_area_postcode_accepted() -> None:
    assert check(Evidence("28195", "Bremen"), osm_postcodes={"28195"}) == "high"
    assert check(Evidence("28195", None), AreaGazetteer(frozenset({"28195"}))) == "high"


def test_no_postcode_in_area_city_accepted() -> None:
    places = AreaGazetteer(frozenset({"28195"}), frozenset({"bremen", "vegesack"}))
    assert check(Evidence(None, "Vegesack"), places) == "high"
    assert check(Evidence(None, "Oldenburg"), places) == "low"


def test_out_of_area_postcode_with_in_area_city_rejected() -> None:
    """A postcode mismatch is final, even when the city is a gazetteer place."""
    gaz = AreaGazetteer(frozenset({"28195", "28199"}), frozenset({"bremen"}))
    assert check(Evidence("80331", "Bremen"), gaz) == "low"
    assert check(Evidence("80331", "Bremen"), osm_postcodes={"28195"}) == "low"


def test_postcode_only_in_footer_list_rejected() -> None:
    """Postcodes in a legal page's footer (branch list) are not the company's own address."""
    html = ("<html><body><main><h1>Impressum</h1><p>Kranich Transporte GmbH</p>"
            "<p>Geschäftsführer: Max Muster</p></main>"
            "<footer><p>Standorte:</p><p>Hafenstr. 1</p><p>28195 Bremen</p>"
            "<p>Elbchaussee 9</p><p>22765 Hamburg</p></footer></body></html>")
    assert company_evidence([page_text(html)], [], PATTERNS, "DE") is not None   # with the footer …
    evidence = company_evidence([page_text(html, drop_footer=True)], [], PATTERNS, "DE")
    assert evidence is None                                                      # … footer-free: none
    assert check(evidence, AreaGazetteer(frozenset({"28195"}), frozenset({"bremen"}))) == "low"


def test_evidence_legal_page_first_then_jsonld() -> None:
    org = JsonLdOrg(types=("Organization",), postal_code="28195", locality="Bremen")
    legal = ["Kranich Transporte GmbH\nLeopoldstr. 5\n80331 München"]
    assert company_evidence(legal, [org], PATTERNS, "DE") == Evidence("80331", "München")
    assert company_evidence(["Impressum ohne Adresse"], [org], PATTERNS, "DE") == Evidence("28195", "Bremen")


def test_gazetteer_without_postcodes_uses_city_rule() -> None:
    places_only = AreaGazetteer(frozenset(), frozenset({"bremen"}))
    assert check(Evidence("28195", "Bremen"), places_only) == "high"
    assert check(Evidence("28195", "Hamburg"), places_only) == "low"


def test_sparse_area_zero_osm_candidates_gazetteer_evidence_accepted() -> None:
    assert check(Evidence("28195", "Bremen"), AreaGazetteer(frozenset({"28195"})), osm_postcodes=()) == "high"


def test_no_gazetteer_decides_with_osm_postcodes_and_area_name() -> None:
    """/ gazetteer failure (``gazetteer=None``): OSM postcodes, then the area-name rule for a
    city-level area only."""
    assert check(Evidence("28195", None), None, {"28195"}, area=COUNTRY) == "high"
    assert check(Evidence("80331", None), None, {"28195"}, area=COUNTRY) == "low"
    assert check(Evidence("40210", "Düsseldorf"), None, (), area=CITY) == "high"
    assert check(Evidence(None, "Bremen"), None, (), area=BREMEN) == "low"      # a state is not city-level
    assert is_city_level(CITY) and not is_city_level(BREMEN) and not is_city_level(COUNTRY)
    assert is_city_level(GeoArea(id="gn:1", country_code="DE", name="Celle", level="Town"))


def test_postcode_token_boundary() -> None:
    assert company_evidence(["Musterweg 1\n128195 Bremen"], [], PATTERNS, "DE") != Evidence("28195", "Bremen")
    assert check(Evidence("128195", "Bremen"), AreaGazetteer(frozenset({"28195"}), frozenset({"bremen"}))) == "low"
    assert normalise_postcode(" sw1a 1aa ") == "SW1A1AA" and normalise_place("Düsseldorf") == "duesseldorf"


# --- gazetteer query (unit) -------------------------------------------------------------------------
def test_gazetteer_query_shape() -> None:
    assert build_gazetteer_query(BREMEN, timeout_s=60) == "\n".join([
        "[out:json][timeout:60];",
        'area["ISO3166-2"="DE-HB"]->.region;',
        "(",
        '  rel["boundary"="postal_code"](area.region);',
        '  node["place"~"^(city|town|village|suburb|hamlet)$"](area.region);',
        ");",
        "out tags;"])
    assert build_gazetteer_query(CITY).splitlines()[1] == "area(id:3600062559)->.region;"


def test_parse_gazetteer() -> None:
    data = {"elements": [
        {"type": "relation", "id": 1, "tags": {"boundary": "postal_code", "postal_code": "28195"}},
        {"type": "relation", "id": 2, "tags": {"boundary": "postal_code", "postal_code": "28199;28201"}},
        {"type": "relation", "id": 3, "tags": {"boundary": "administrative", "postal_code": "99999"}},
        {"type": "node", "id": 4, "tags": {"place": "city", "name": "Bremen"}},
        {"type": "node", "id": 5, "tags": {"place": "suburb", "name": "Vegesack"}},
        {"type": "node", "id": 6, "tags": {"place": "locality", "name": "Weserwiese"}},
        {"type": "node", "id": 7, "tags": {"place": "village"}},
    ]}
    gaz = parse_gazetteer(data)
    assert gaz.postcodes == {"28195", "28199", "28201"} and gaz.places == {"bremen", "vegesack"}
    assert parse_gazetteer({}) == AreaGazetteer()


def test_gb_split_area_level_is_not_lowercase_country() -> None:
    """Uses ``level == "country"`` exactly: GB's split areas have level ``"Country"``."""
    from leadscraper.services.planner import country_split
    gb = [a for a in country_split(GeoArea(id="iso:GB", country_code="GB", name="United Kingdom",
                                          level="country")) if a.code == "GB-ENG"]
    assert gb and gb[0].level == "Country" and gb[0].level != "country"
