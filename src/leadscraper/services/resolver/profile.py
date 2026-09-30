"""CountryProfile, built for any country from open datasets."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml

from leadscraper import constants as C

#: adapters actually available in v0.3.
ENABLED_SOURCES: tuple[str, ...] = ("osm",)


# --- verbatim (src/leadscraper/services/resolver/profile.py, excerpt) ---------------------------
import re
from dataclasses import dataclass, field

from babel.languages import get_official_languages
from i18naddress import get_validation_rules

EU_EEA = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
          "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO"}
OPEN_REGISTERS = {"GB": "gb_companies_house", "FR": "fr_recherche_entreprises", "NO": "no_brreg"}

@dataclass(slots=True)
class CountryProfile:
    code: str
    languages: tuple[str, ...]
    postal_patterns: list[re.Pattern]
    contact_keywords: list[str]
    sources: list[str]
    tier: str                                    # A: open register, B: legal notice mandatory, C: others
    overrides: dict = field(default_factory=dict)


def build_country_profile(cc: str, contact_words: dict[str, list[str]],
                          overrides: dict | None = None,
                          enabled_sources: Iterable[str] = ENABLED_SOURCES) -> CountryProfile:
    overrides = overrides or {}
    langs = tuple(get_official_languages(cc)) or ("en",)           # CLDR: CH -> ('de', 'fr', 'it')
    rules = get_validation_rules({"country_code": cc})              # Google address data (offline)
    postal = [re.compile(m.pattern.lstrip("^").rstrip("$")) for m in rules.postal_code_matchers]
    words = [w for lang in (*langs, "en") for w in contact_words.get(lang, [])]
    sources = ["google_places", "osm"] + ([OPEN_REGISTERS[cc]] if cc in OPEN_REGISTERS else [])
    enabled = set(enabled_sources)                                  # only enabled adapters
    sources = [s for s in sources if s in enabled]
    tier = "A" if cc in OPEN_REGISTERS else "B" if cc in EU_EEA | {"CH"} else "C"
    profile = CountryProfile(cc, langs, postal, list(dict.fromkeys(words)), sources, tier, overrides)
    for key, value in overrides.items():         # override YAML always wins
        if hasattr(profile, key):
            setattr(profile, key, value)
    return profile
# --- end of verbatim ----------------------------------------------------------------------------

CONTACT_PAGES_FILE = C.CONFIG_DIR / "i18n" / "contact_pages.yaml"
WEB_SEARCH_SOURCE = "web_search"                   # = sources.web_search.SOURCE_NAME
COUNTRY_OVERRIDES_DIR = C.CONFIG_DIR / "overrides" / "countries"


@dataclass(slots=True, frozen=True)
class ContactPages:
    keywords: dict[str, list[str]]
    fallback_paths: tuple[str, ...]
    legal_markers: tuple[str, ...] = ()
    contact_markers: tuple[str, ...] = ()
    #: kind ("legal" | "contact") → language → paths
    fallback_by_kind: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)


def load_contact_pages(path: Path | str = CONTACT_PAGES_FILE) -> ContactPages:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    keywords = {str(lang).lower(): [str(w).lower() for w in words or []]
                for lang, words in (raw.get("keywords") or {}).items()}
    paths = tuple(str(p) for p in raw.get("fallback_paths") or [])
    markers = tuple(str(m).lower() for m in raw.get("legal_markers") or [])
    contact = tuple(str(m).lower() for m in raw.get("contact_markers") or [])
    by_kind = {str(kind): {str(lang).lower(): tuple(str(p) for p in ps or [])
                           for lang, ps in (langs or {}).items()}
               for kind, langs in (raw.get("fallback_paths_by_kind") or {}).items()}
    return ContactPages(keywords, paths, markers, contact, by_kind)


def load_country_overrides(cc: str, directory: Path | str = COUNTRY_OVERRIDES_DIR) -> dict[str, Any]:
    """Read ``<CC>.yaml`` and coerce values to the ``CountryProfile`` field types."""
    p = Path(directory) / f"{cc.upper()}.yaml"
    if not p.is_file():
        return {}
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if "postal_patterns" in raw:
        raw["postal_patterns"] = [re.compile(str(x)) for x in raw["postal_patterns"] or []]
    if "languages" in raw:
        raw["languages"] = tuple(str(x) for x in raw["languages"] or [])
    if "contact_keywords" in raw:
        raw["contact_keywords"] = [str(x).lower() for x in raw["contact_keywords"] or []]
    return raw


class ProfileBuilder:
    """Builds and memoises CountryProfiles."""

    def __init__(self, contact_pages: ContactPages | None = None,
                 overrides_dir: Path | str = COUNTRY_OVERRIDES_DIR,
                 enabled_sources: Iterable[str] = ENABLED_SOURCES) -> None:
        self.contact_pages = contact_pages or load_contact_pages()
        self.overrides_dir = Path(overrides_dir)
        self.enabled_sources = tuple(enabled_sources)
        self._cache: dict[str, CountryProfile] = {}

    def get(self, cc: str) -> CountryProfile:
        cc = cc.upper()
        if cc not in self._cache:
            profile = build_country_profile(
                cc, self.contact_pages.keywords, load_country_overrides(cc, self.overrides_dir),
                self.enabled_sources)
            # keyless web-search discovery works for every country; it is added here whenever it is
            # an enabled source
            if WEB_SEARCH_SOURCE in self.enabled_sources and WEB_SEARCH_SOURCE not in profile.sources:
                profile.sources.append(WEB_SEARCH_SOURCE)
            self._cache[cc] = profile
        return self._cache[cc]

    @property
    def fallback_paths(self) -> tuple[str, ...]:
        return self.contact_pages.fallback_paths

    def fallback_paths_for(self, languages: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(legal, contact)`` fallback paths per kind: the country's languages in profile order,
        then ``en``, deduplicated."""
        out: list[tuple[str, ...]] = []
        for kind in ("legal", "contact"):
            table = self.contact_pages.fallback_by_kind.get(kind, {})
            paths = [p for lang in (*languages, "en") for p in table.get(lang, ())]
            out.append(tuple(dict.fromkeys(paths)))
        return out[0], out[1]
