"""CountryProfile (ARCHITECTURE.md §3.2), built for any country from open datasets.

The block between the ``--- A§3.2 verbatim ---`` markers is the A§3.2 code with exactly one
intended change (PLAN.md Q8, Supervisor): ``sources`` lists only *enabled* adapters, so the default
is ``["osm"]``. ``tier`` is computed exactly as in A§3.2 (informational yield expectation).
Below the block: loaders for ``config/i18n/contact_pages.yaml`` and
``config/overrides/countries/<CC>.yaml`` (override YAML always wins).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from leadscraper import constants as C

#: Q8: adapters actually available in v0.3 (sources/registry.py, T11 may pass its own list).
ENABLED_SOURCES: tuple[str, ...] = ("osm",)


# --- A§3.2 verbatim (src/leadscraper/services/resolver/profile.py, excerpt) ---------------------
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
    enabled = set(enabled_sources)                                  # Q8: only enabled adapters
    sources = [s for s in sources if s in enabled]
    tier = "A" if cc in OPEN_REGISTERS else "B" if cc in EU_EEA | {"CH"} else "C"
    profile = CountryProfile(cc, langs, postal, list(dict.fromkeys(words)), sources, tier, overrides)
    for key, value in overrides.items():         # override YAML always wins
        if hasattr(profile, key):
            setattr(profile, key, value)
    return profile
# --- end of A§3.2 verbatim ----------------------------------------------------------------------

CONTACT_PAGES_FILE = C.CONFIG_DIR / "i18n" / "contact_pages.yaml"
COUNTRY_OVERRIDES_DIR = C.CONFIG_DIR / "overrides" / "countries"


@dataclass(slots=True, frozen=True)
class ContactPages:
    keywords: dict[str, list[str]]
    fallback_paths: tuple[str, ...]
    legal_markers: tuple[str, ...] = ()


def load_contact_pages(path: Path | str = CONTACT_PAGES_FILE) -> ContactPages:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    keywords = {str(lang).lower(): [str(w).lower() for w in words or []]
                for lang, words in (raw.get("keywords") or {}).items()}
    paths = tuple(str(p) for p in raw.get("fallback_paths") or [])
    markers = tuple(str(m).lower() for m in raw.get("legal_markers") or [])
    return ContactPages(keywords, paths, markers)


def load_country_overrides(cc: str, directory: Path | str = COUNTRY_OVERRIDES_DIR) -> dict[str, Any]:
    """Read ``<CC>.yaml`` and coerce values to the ``CountryProfile`` field types.

    ``postal_patterns`` strings are compiled, ``languages`` becomes a tuple; unknown keys (e.g.
    ``compliance_note``) are passed through and end up in ``CountryProfile.overrides`` only.
    """
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
    """Builds and memoises CountryProfiles (static data, process lifetime — not job data, C7)."""

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
            self._cache[cc] = build_country_profile(
                cc, self.contact_pages.keywords, load_country_overrides(cc, self.overrides_dir),
                self.enabled_sources)
        return self._cache[cc]

    @property
    def fallback_paths(self) -> tuple[str, ...]:
        return self.contact_pages.fallback_paths
