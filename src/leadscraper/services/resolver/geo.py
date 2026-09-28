"""Country & region resolution (ARCHITECTURE.md §3.2).

The block between the ``--- A§3.2 verbatim ---`` markers is the A§3.2 code, unchanged.
Everything below it is glue: alias loading, method reporting, ``GeoArea`` construction, index
warm-up and the optional Nominatim fallback (only when ``NOMINATIM_URL`` is set; Q7).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from aiolimiter import AsyncLimiter

from leadscraper import constants as C
from leadscraper.domain.models import GeoArea
from leadscraper.observability.logging import get_logger

# --- A§3.2 verbatim (src/leadscraper/services/resolver/geo.py, excerpt) ------------------------
import unicodedata
from functools import lru_cache

import pycountry
from babel import Locale, localedata
from rapidfuzz import fuzz, process


def norm(s: str) -> str:
    """'Île-de-France' -> 'ile de france' (lowercase, no accents, hyphens become spaces)."""
    s = unicodedata.normalize("NFKD", s.replace("ß", "ss"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.casefold().replace("-", " ").replace("_", " ").split())


def _pick(q: str, choices: dict[str, str], auto: float = 90, margin: float = 5, floor: float = 70):
    """Fuzzy match: accept automatically only if the score is high AND clearly ahead of the 2nd candidate."""
    hits = process.extract(q, choices, scorer=fuzz.ratio, limit=3)
    if not hits:
        return None, []
    (_, best, key), *rest = hits
    second = rest[0][1] if rest else 0
    if best >= auto and best - second >= margin:
        return key, []
    return None, [k for _, s, k in hits if s >= floor]          # ambiguous -> becomes suggestions in the 422 error


@lru_cache
def _country_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for c in pycountry.countries:
        for key in (c.alpha_2, c.alpha_3, c.name,
                    getattr(c, "official_name", None), getattr(c, "common_name", None)):
            if key:
                idx[norm(key)] = c.alpha_2
    for loc in localedata.locale_identifiers():                   # country names in all CLDR languages
        for code, name in Locale.parse(loc).territories.items():
            if len(code) == 2 and code.isalpha():
                idx.setdefault(norm(name), code)
    return idx


def resolve_country(text: str, aliases: dict[str, str] | None = None) -> tuple[str | None, list[str]]:
    q, idx = norm(text), _country_index()
    if q in idx:
        return idx[q], []
    if aliases and q in aliases:
        return aliases[q], []
    key, suggestions = _pick(q, {k: k for k in idx})
    return (idx[key] if key else None), sorted({idx[s] for s in suggestions})


def resolve_region(country: str, text: str, aliases: dict[str, str] | None = None):
    subs = {s.code: s.name for s in pycountry.subdivisions.get(country_code=country) or []}
    q = norm(text)
    for code, name in subs.items():                               # "DE-BY", "BY", "Bayern"
        if q in (norm(code), norm(code.split("-", 1)[1]), norm(name)):
            return code, []
    if aliases and aliases.get(q) in subs:                        # curated alias / GeoNames alternateNames
        return aliases[q], []
    code, suggestions = _pick(q, {c: norm(n) for c, n in subs.items()})
    return code, suggestions   # None -> continue with GeoNames admin2/city, then Nominatim (not shown)
# --- end of A§3.2 verbatim ----------------------------------------------------------------------

log = get_logger(__name__)

ALIASES_FILE = C.CONFIG_DIR / "overrides" / "aliases.yaml"


@dataclass(slots=True, frozen=True)
class Aliases:
    countries: dict[str, str] = field(default_factory=dict)            # norm(name) -> "GB"
    regions: dict[str, dict[str, str]] = field(default_factory=dict)   # "DE" -> norm(name) -> "DE-NW"
    industries: dict[str, str] = field(default_factory=dict)


def load_aliases(path: Path | str = ALIASES_FILE) -> Aliases:
    """Read ``aliases.yaml``; keys are normalised with :func:`norm`, codes upper-cased."""
    p = Path(path)
    raw: dict[str, Any] = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.is_file() else {}
    countries = {norm(str(k)): str(v).upper() for k, v in (raw.get("countries") or {}).items()}
    regions = {
        str(cc).upper(): {norm(str(k)): str(v).upper() for k, v in (entries or {}).items()}
        for cc, entries in (raw.get("regions") or {}).items()
    }
    industries = {norm(str(k)): str(v) for k, v in (raw.get("industries") or {}).items()}
    return Aliases(countries, regions, industries)


@lru_cache(maxsize=1)
def default_aliases() -> Aliases:
    return load_aliases()


def warm_up() -> int:
    """Build the CLDR country index and load ISO 3166-2 data (called once at startup)."""
    idx = _country_index()
    pycountry.subdivisions.get(code="DE-BY")
    return len(idx)


# --- Country --------------------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class CountryResolution:
    input: str
    code: str | None
    method: str | None                    # exact | alias | fuzzy | None (unresolved)
    suggestions: tuple[str, ...] = ()     # alpha-2 codes

    @property
    def name(self) -> str | None:
        c = pycountry.countries.get(alpha_2=self.code) if self.code else None
        return c.name if c else None


def resolve_country_detailed(text: str, aliases: Aliases | None = None) -> CountryResolution:
    aliases = default_aliases() if aliases is None else aliases
    code, suggestions = resolve_country(text, aliases.countries)
    q = norm(text)
    if code is None:
        return CountryResolution(text, None, None, tuple(suggestions))
    method = "exact" if q in _country_index() else "alias" if q in aliases.countries else "fuzzy"
    return CountryResolution(text, code, method)


# --- Region ---------------------------------------------------------------------------------------
def _subdivision(code: str):
    return pycountry.subdivisions.get(code=code)


def geo_area_for_code(code: str, *, input: str = "", method: str = "exact") -> GeoArea:
    sub = _subdivision(code)
    return GeoArea(id=f"iso:{code}", country_code=code.split("-", 1)[0], name=sub.name,
                   level=str(sub.type).lower(), input=input, method=method, code=code)


def country_area(cc: str, *, input: str = "") -> GeoArea:
    """Whole-country area used when ``regions`` is empty (A§2.1: empty = whole country)."""
    c = pycountry.countries.get(alpha_2=cc)
    return GeoArea(id=f"iso:{cc}", country_code=cc, name=c.name if c else cc, level="country",
                   input=input, method="country", code=cc)


def region_suggestion(code: str) -> dict[str, str]:
    """``{"id": "iso:DE-BY", "name": "Bayern"}`` as in the A§2.3 error example."""
    return {"id": f"iso:{code}", "name": _subdivision(code).name}


@dataclass(slots=True, frozen=True)
class RegionResolution:
    input: str
    area: GeoArea | None
    suggestions: tuple[str, ...] = ()     # ISO 3166-2 codes

    @property
    def method(self) -> str | None:
        return self.area.method if self.area else None


def resolve_region_detailed(country: str, text: str, aliases: Aliases | None = None) -> RegionResolution:
    aliases = default_aliases() if aliases is None else aliases
    country_aliases = aliases.regions.get(country.upper(), {})
    code, suggestions = resolve_region(country, text, country_aliases)
    if code is None:
        return RegionResolution(text, None, tuple(suggestions))
    q = norm(text)
    sub = _subdivision(code)
    exact = q in (norm(code), norm(code.split("-", 1)[1]), norm(sub.name))
    method = "exact" if exact else "alias" if country_aliases.get(q) == code else "fuzzy"
    return RegionResolution(text, geo_area_for_code(code, input=text, method=method))


def list_regions(country: str) -> list[dict[str, str]]:
    """ISO 3166-2 subdivisions for ``GET /meta/regions?country=..``."""
    subs = pycountry.subdivisions.get(country_code=country.upper()) or []
    return sorted(({"id": f"iso:{s.code}", "code": s.code, "name": s.name, "level": str(s.type).lower(),
                    "parent": f"iso:{s.parent_code}" if getattr(s, "parent_code", None) else None}
                   for s in subs), key=lambda d: d["code"])


def list_countries() -> list[dict[str, str]]:
    return sorted(({"code": c.alpha_2, "alpha_3": c.alpha_3, "name": c.name}
                   for c in pycountry.countries), key=lambda d: d["code"])


# --- Optional Nominatim fallback (Q7) -------------------------------------------------------------
class NominatimClient:
    """Last-resort region lookup; used only when ``NOMINATIM_URL`` is set.

    Rate-limited to ``NOMINATIM_MAX_RPS``; bounded process-lifetime LRU cache of geodata only
    (never job data). A hit is accepted only when the returned name matches the query closely
    (conservative, C13) and the result is an OSM relation (needed for ``area(id:...)``, A§4).
    """

    ACCEPT_SCORE = C.NOMINATIM_ACCEPT_SCORE

    def __init__(self, base_url: str, *, max_rps: float, user_agent: str,
                 client: httpx.AsyncClient | None = None,
                 cache_size: int = C.NOMINATIM_CACHE_MAXSIZE) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self._client = client
        self._limiter = AsyncLimiter(max_rps, 1) if max_rps >= 1 else AsyncLimiter(1, 1 / max_rps)
        self._cache: OrderedDict[tuple[str, str], GeoArea | None] = OrderedDict()
        self._cache_size = cache_size
        self._lock = asyncio.Lock()

    async def lookup(self, country: str, text: str) -> GeoArea | None:
        key = (country.upper(), norm(text))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        area = await self._fetch(country.upper(), text)
        self._cache[key] = area
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return area

    async def _fetch(self, country: str, text: str) -> GeoArea | None:
        params = {"q": text, "countrycodes": country.lower(), "format": "jsonv2", "limit": "5",
                  "addressdetails": "0", "namedetails": "1"}
        client = self._client or httpx.AsyncClient(timeout=C.NOMINATIM_HTTP_TIMEOUT_S)
        try:
            async with self._lock, self._limiter:
                resp = await client.get(f"{self.base_url}/search", params=params,
                                        headers={"User-Agent": self.user_agent})
            resp.raise_for_status()
            results = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("nominatim_failed", error=type(exc).__name__)
            return None
        finally:
            if self._client is None:
                await client.aclose()
        q = norm(text)
        for r in results if isinstance(results, list) else []:
            if r.get("osm_type") != "relation":
                continue
            name = r.get("name") or str(r.get("display_name", "")).split(",")[0]
            if fuzz.ratio(q, norm(name)) < self.ACCEPT_SCORE:
                continue
            bbox = r.get("boundingbox")
            box = None
            if isinstance(bbox, list) and len(bbox) == 4:
                s, n, w, e = (float(x) for x in bbox)
                box = (s, w, n, e)
            rel = int(r["osm_id"])
            return GeoArea(id=f"osm:r{rel}", country_code=country, name=name,
                           level=str(r.get("addresstype") or r.get("type") or "area"),
                           input=text, method="nominatim", osm_relation_id=rel, bbox=box)
        return None
