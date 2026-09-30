import re
from pathlib import Path

import pytest

from leadscraper.services.resolver.profile import (
    ProfileBuilder,
    build_country_profile,
    load_contact_pages,
    load_country_overrides,
)

PAGES = load_contact_pages()


@pytest.fixture(scope="module")
def builder() -> ProfileBuilder:
    return ProfileBuilder()


def test_contact_pages_languages_and_fallbacks() -> None:
    assert set(PAGES.keywords) >= {"de", "en", "fr", "es", "it", "nl", "pt"}
    assert PAGES.fallback_paths[:4] == ("/impressum", "/mentions-legales", "/aviso-legal",
                                        "/contact")


def test_de(builder: ProfileBuilder) -> None:
    p = builder.get("DE")
    assert p.languages == ("de",)
    assert "impressum" in p.contact_keywords and "kontakt" in p.contact_keywords
    assert "contact" in p.contact_keywords                       # English always added
    assert p.contact_keywords.index("impressum") < p.contact_keywords.index("contact")
    assert any(pat.fullmatch("80331") for pat in p.postal_patterns)
    assert not any(pat.fullmatch("8033") for pat in p.postal_patterns)
    assert p.tier == "B"
    assert p.sources == ["osm"]
    assert "DSGVO" in p.overrides["compliance_note"]           # extra keys kept in overrides


def test_ch_three_languages(builder: ProfileBuilder) -> None:
    p = builder.get("CH")
    assert p.languages == ("de", "fr", "it")
    assert {"impressum", "mentions légales", "contatti"} <= set(p.contact_keywords)
    assert p.tier == "B"


def test_us_tier_c(builder: ProfileBuilder) -> None:
    p = builder.get("US")
    assert p.languages == ("en",)
    assert "imprint" in p.contact_keywords and "contact us" in p.contact_keywords
    assert p.tier == "C"
    assert p.sources == ["osm"]


def test_fr_tier_a_but_register_not_listed(builder: ProfileBuilder) -> None:
    p = builder.get("fr")
    assert p.code == "FR" and p.tier == "A"
    assert p.sources == ["osm"]                                  # register adapter not enabled
    assert any(pat.fullmatch("75008") for pat in p.postal_patterns)


def test_sources_follow_enabled_adapters() -> None:
    p = build_country_profile("FR", PAGES.keywords,
                              enabled_sources=("osm", "google_places", "fr_recherche_entreprises"))
    assert p.sources == ["google_places", "osm", "fr_recherche_entreprises"]
    assert build_country_profile("US", PAGES.keywords).sources == ["osm"]


def test_country_without_official_language_falls_back_to_en() -> None:
    p = build_country_profile("US", PAGES.keywords)
    assert p.languages == ("en",) and "contact" in p.contact_keywords and p.tier == "C"


def test_override_yaml_wins(tmp_path: Path) -> None:
    (tmp_path / "DE.yaml").write_text(
        "tier: A\nlanguages: [de, en]\ncontact_keywords: [Impressum]\n"
        "postal_patterns: ['\\d{3}']\ncompliance_note: x\nunknown_field: 1\n", encoding="utf-8")
    p = ProfileBuilder(PAGES, overrides_dir=tmp_path).get("DE")
    assert p.tier == "A" and p.languages == ("de", "en") and p.contact_keywords == ["impressum"]
    assert [x.pattern for x in p.postal_patterns] == [r"\d{3}"]
    assert isinstance(p.postal_patterns[0], re.Pattern)
    assert p.overrides["compliance_note"] == "x" and not hasattr(p, "unknown_field")


def test_missing_override_file(tmp_path: Path) -> None:
    assert load_country_overrides("ZZ", tmp_path) == {}


def test_seeded_compliance_notes() -> None:
    for cc in ("DE", "FR", "GB"):
        assert load_country_overrides(cc)["compliance_note"].startswith(f"{cc}:")


def test_builder_memoises(builder: ProfileBuilder) -> None:
    assert builder.get("DE") is builder.get("de")
