import re

import pytest

from leadscraper.domain.models import GeoArea, IndustryProfile
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.industry import default_catalog
from leadscraper.sources.osm_overpass import (
    area_selector,
    build_query,
    element_to_candidate,
    is_excluded,
    keyword_pattern,
    ql_string,
    regex_escape,
)

A4_EXAMPLE = """[out:json][timeout:180];
area["ISO3166-2"="DE-NW"]->.region;
(
  nwr["name"~"fabrik|produktion|hersteller",i](area.region);
  nwr["man_made"="works"](area.region);
);
out tags center;"""

NRW = geo.resolve_region_detailed("DE", "Nordrhein-Westfalen").area


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def test_a4_example_reproduced_exactly_without_extras() -> None:
    profile = IndustryProfile(id="t", input="Produktion", isic=("C",),
                              keywords={"de": ("fabrik", "produktion", "hersteller")},
                              osm_tags=(("man_made", "works"),))
    q = build_query(NRW, profile, ("de",), word_boundaries=False, require_name=False,
                    named_set=False)
    assert _norm_ws(q) == _norm_ws(A4_EXAMPLE)


def test_a4_example_semantics_with_catalog_profile() -> None:
    profile = default_catalog().resolve("Produktion", ("de",)).profile
    q = build_query(NRW, profile, ("de",))
    assert q.startswith("[out:json][timeout:180];")
    assert 'area["ISO3166-2"="DE-NW"]->.region;' in q
    assert 'nwr(area.region)["name"]->.named;' in q              # named-set form (T25)
    name_line = next(line for line in q.splitlines() if '"name"~' in line)
    for kw in ("fabrik", "produktion", "fertigung", "hersteller"):
        assert kw in name_line
    assert name_line.strip().startswith('nwr.named["name"~') and ",i]" in name_line
    assert 'nwr.named["man_made"="works"];' in q
    assert '["name"!~"handwerk|werkstatt|feuerwerk|stadtwerk",i]' in name_line
    assert q.rstrip().endswith("out tags center;")


def test_short_keywords_need_whole_word_long_ones_are_substrings() -> None:
    pat = keyword_pattern(["werk", "lager", "maschinenbau"])
    assert pat.startswith("(^|[^[:alnum:]])werk([^[:alnum:]]|$)|")
    assert "(^|[^[:alnum:]])lager([^[:alnum:]]|$)" in pat
    assert pat.endswith("|maschinenbau")
    # the ERE semantics, checked with Python re (POSIX classes translated)
    py = re.compile(pat.replace("[:alnum:]", r"\w"), re.I)
    assert py.search("Stahl Werk Süd") and py.search("Lager & Logistik GmbH")
    assert not py.search("Handwerk Demo") and not py.search("Werkstatt Übungs")
    assert not py.search("Bierlager")
    assert py.search("Sondermaschinenbau Muster")      # compound still found


def test_keywords_are_regex_and_ql_escaped() -> None:
    assert regex_escape("b.v. (x)|y*") == r"b\.v\. \(x\)\|y\*"
    assert ql_string('a"b\\c') == 'a\\"b\\\\c'
    profile = IndustryProfile(id="t", input="x", isic=("C",),
                              keywords={"de": ('übungs "&" söhne', "a.b+c")})
    q = build_query(NRW, profile, ("de",))
    line = next(line for line in q.splitlines() if '"name"~' in line)
    assert 'übungs \\"&\\" söhne' in line          # quote escaped for QL
    assert "a\\\\.b\\\\+c" in line                  # regex escape, then QL escape


def test_duplicate_and_blank_keywords_removed() -> None:
    assert keyword_pattern(["Logistik", "logistik", " ", ""], word_boundaries=False) == "logistik"


def test_area_selectors() -> None:
    assert area_selector(geo.country_area("DE")) == 'area["ISO3166-1"="DE"][admin_level=2]->.region;'
    rel = GeoArea(id="osm:r62428", country_code="DE", name="München", level="city",
                  method="nominatim", osm_relation_id=62428)
    assert area_selector(rel) == "area(id:3600062428)->.region;"
    with pytest.raises(ValueError):
        area_selector(GeoArea(id="gn:1", country_code="DE", name="x", level="city"))


def test_limit_and_timeout() -> None:
    profile = default_catalog().resolve("Logistik", ("de",)).profile
    q = build_query(geo.resolve_region_detailed("DE", "Bayern").area, profile, ("de",), limit=150)
    assert q.rstrip().endswith("out tags center 150;")
    assert 'nwr.named["office"="logistics"];' in q


def test_profile_without_keywords_only_tag_clauses() -> None:
    profile = IndustryProfile(id="t", input="x", isic=("C",), osm_tags=(("shop", "wholesale"),))
    q = build_query(NRW, profile, ("de",))
    assert '"name"~' not in q and 'nwr.named["shop"="wholesale"];' in q
    legacy = build_query(NRW, profile, ("de",), named_set=False)
    assert 'nwr["shop"="wholesale"]["name"](area.region);' in legacy


def test_element_mapping() -> None:
    node = element_to_candidate({"type": "node", "id": 5, "lat": 1.5, "lon": 2.5, "tags": {
        "name": " Firma GmbH ", "contact:website": "firma-example.de", "contact:email": "info@firma-example.de",
        "addr:postcode": "80331", "phone": "+49 89 1"}})
    assert node.name == "Firma GmbH" and node.source == "osm" and node.source_ref == "node/5"
    assert node.website == "firma-example.de" and node.postal_code == "80331"
    assert (node.lat, node.lon, node.coords_storable) == (1.5, 2.5, True)
    assert node.hints == {"email": "info@firma-example.de", "phone": "+49 89 1"}
    way = element_to_candidate({"type": "way", "id": 7, "center": {"lat": 3, "lon": 4},
                                "tags": {"name": "X", "website": "https://x-example.de", "contact:website": "y"}})
    assert way.website == "https://x-example.de" and (way.lat, way.lon) == (3.0, 4.0)
    assert element_to_candidate({"type": "node", "id": 1, "tags": {}}) is None
    assert element_to_candidate({"type": "area", "id": 1, "tags": {"name": "x"}}) is None


def test_negative_filter() -> None:
    assert is_excluded("Autowerkstatt Demo", ["werkstatt"])
    assert not is_excluded("Maschinenbau Demo", ["werkstatt"])


def test_no_api_key_read_in_sources() -> None:
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "leadscraper" / "sources"
    for f in src.rglob("*.py"):
        code = f.read_text(encoding="utf-8")
        assert "api_key" not in code and "API_KEY" not in code, f.name


def test_named_set_form_is_equivalent_to_a4_shape() -> None:
    """T25 live diagnosis: same filters, but the regex runs on the area's named elements only."""
    profile = default_catalog().resolve("Logistik", ("de",)).profile
    bremen = geo.resolve_region_detailed("DE", "Bremen").area
    q = build_query(bremen, profile, ("de",), limit=50)
    assert q.splitlines() == [
        "[out:json][timeout:180];",
        'area["ISO3166-2"="DE-HB"]->.region;',
        'nwr(area.region)["name"]->.named;',
        "(",
        '  nwr.named["name"~"logistik|spedition|(^|[^[:alnum:]])lager([^[:alnum:]]|$)|transport",i]'
        '["name"!~"lagerverkauf|zeltlager|ferienlager|lagerfeuer|krankentransport",i];',
        '  nwr.named["office"="logistics"];',
        ");",
        "out tags center 50;",
    ]


def test_client_timeout_exceeds_server_timeout() -> None:
    from leadscraper import constants as C

    assert C.OVERPASS_HTTP_TIMEOUT_S > C.OVERPASS_QUERY_TIMEOUT_S
    assert C.OVERPASS_HTTP_TIMEOUT_S - C.OVERPASS_QUERY_TIMEOUT_S >= 30
