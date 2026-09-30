import pytest

from leadscraper.services.resolver.geo import Aliases
from leadscraper.services.resolver.industry import (
    IndustryCatalog,
    default_catalog,
    keywords_for,
    load_catalog,
)


@pytest.fixture(scope="module")
def cat() -> IndustryCatalog:
    return default_catalog()


@pytest.mark.parametrize("text,isic", [
    ("Maschinenbau", ("28",)),
    ("Produktion", ("C",)),
    ("Logistik", ("49", "52", "53")),
    ("Großhandel", ("46",)),
    ("Grosshandel", ("46",)),
    ("Herstellung", ("C",)),
    ("Transport routier", ("49",)),
    ("Commerce de gros", ("46",)),
    ("wholesale", ("46",)),
    ("Manufacturing", ("C",)),
    ("Manufacture of furniture", ("31",)),       # official ISIC title
    ("28", ("28",)),
    ("ISIC 46", ("46",)),
    ("c", ("C",)),
])
def test_catalog_golden(cat: IndustryCatalog, text: str, isic: tuple[str, ...]) -> None:
    res = cat.resolve(text, ("de",))
    assert res.profile is not None, res.suggestions
    assert res.profile.isic == isic
    assert res.profile.method == "catalog" and res.warning is None
    assert res.profile.input == text
    assert (res.profile.scheme, res.profile.version) == ("ISIC", "Rev.4")


def test_manufacturing_german_keywords(cat: IndustryCatalog) -> None:
    p = cat.resolve("Produktion", ("de",)).profile
    assert keywords_for(p, ("de",)) == {"de": ["fabrik", "werk", "produktion", "fertigung", "hersteller"]}
    assert ("man_made", "works") in p.osm_tags


def test_logistics_french_keywords(cat: IndustryCatalog) -> None:
    p = cat.resolve("Logistique", ("fr",)).profile
    assert keywords_for(p, ("fr",)) == {"fr": ["logistique", "transport", "entreposage", "messagerie"]}


def test_keywords_fall_back_to_english(cat: IndustryCatalog) -> None:
    p = cat.resolve("Maschinenbau").profile
    assert keywords_for(p, ("sw",)) == {"en": ["machinery", "machine", "machines"]}
    assert keywords_for(p, ("de", "fr"))["de"][0] == "maschinenbau"


def test_fuzzy_typo_accepted_with_warning(cat: IndustryCatalog) -> None:
    res = cat.resolve("Maschinenbauu")
    assert res.profile is not None and res.profile.isic == ("28",)
    assert res.method == "fuzzy" and res.warning


@pytest.mark.parametrize("text", ["xqzvbnm", "Blorptastic Wibble", "Handel"])
def test_unresolved_returns_suggestions(cat: IndustryCatalog, text: str) -> None:
    res = cat.resolve(text)
    assert res.profile is None and res.method is None
    assert 1 <= len(res.suggestions) <= 3
    for s in res.suggestions:
        assert set(s) == {"isic", "title"} and s["title"] == cat.titles[s["isic"]]


def test_handel_is_ambiguous_not_guessed(cat: IndustryCatalog) -> None:
    """'Handel' (trade) could be wholesale or retail -> never guessed."""
    codes = {s["isic"] for s in cat.resolve("Handel").suggestions}
    assert codes  # suggestions exist, but nothing was auto-accepted


def test_operator_alias(tmp_path) -> None:
    catalog = load_catalog(aliases=Aliases(industries={"frachtdienst": "logistics", "baumaschinen": "28"}))
    assert catalog.resolve("Frachtdienst").profile.isic == ("49", "52", "53")
    assert catalog.resolve("Frachtdienst").method == "alias"
    assert catalog.resolve("Baumaschinen").profile.isic == ("28",)


def test_catalog_integrity(cat: IndustryCatalog) -> None:
    assert cat.collisions == []                       # no alias maps to two concepts
    assert len([c for c in cat.titles if c.isalpha()]) == 21
    assert len([c for c in cat.titles if c.isdigit()]) == 88
    for entry in cat.entries.values():
        assert entry.isic and all(code in cat.titles for code in entry.isic), entry.id
    for required in ("de", "en", "fr"):
        for eid in ("manufacturing", "machinery", "logistics", "wholesale", "road_transport"):
            assert cat.entries[eid].keywords.get(required), (eid, required)


def test_search_for_meta(cat: IndustryCatalog) -> None:
    hits = cat.search("logistik")
    assert hits and hits[0]["id"] == "logistics" and hits[0]["isic"] == ["49", "52", "53"]
    assert cat.search("")


def test_resolution_is_offline(cat: IndustryCatalog) -> None:
    import respx

    with respx.mock(assert_all_mocked=True):
        assert cat.resolve("Logistik").profile is not None
