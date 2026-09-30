"""Industry resolution."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz, process

from leadscraper import constants as C
from leadscraper.domain.models import IndustryProfile
from leadscraper.services.resolver.geo import Aliases, _pick, default_aliases, norm

CATALOG_FILE = C.DATA_DIR / "isic" / "industries.yaml"
SUGGESTION_LIMIT = C.INDUSTRY_SUGGESTION_LIMIT
_CODE_RE = re.compile(r"^(?:isic[\s:]*)?([a-u]|\d{2})$", re.I)


@dataclass(slots=True, frozen=True)
class CatalogEntry:
    """One resolvable catalog concept (a curated profile or a bare ISIC section/division)."""

    id: str
    isic: tuple[str, ...]
    title: str
    keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)
    negative_keywords: tuple[str, ...] = ()
    osm_tags: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True, frozen=True)
class IndustryResolution:
    input: str
    profile: IndustryProfile | None
    suggestions: tuple[dict[str, str], ...] = ()     # [{"isic": "46", "title": "..."}]
    warning: str | None = None

    @property
    def method(self) -> str | None:
        return self.profile.method if self.profile else None


class IndustryCatalog:
    def __init__(self, data: dict[str, Any], aliases: Aliases | None = None) -> None:
        self.scheme = str(data.get("scheme", "ISIC"))
        self.version = str(data.get("version", "Rev.4"))
        isic = data.get("isic") or {}
        self.titles: dict[str, str] = {
            **{str(k).upper(): str(v) for k, v in (isic.get("sections") or {}).items()},
            **{str(k).zfill(2): str(v) for k, v in (isic.get("divisions") or {}).items()},
        }
        self.entries: dict[str, CatalogEntry] = {}
        self.index: dict[str, str] = {}                  # norm(alias/title) -> entry id
        self.collisions: list[tuple[str, str, str]] = []
        for p in data.get("profiles") or []:
            codes = tuple(str(c).upper() if not str(c).isdigit() else str(c).zfill(2)
                          for c in p["isic"])
            entry = CatalogEntry(
                id=str(p["id"]), isic=codes,
                title=" / ".join(self.titles.get(c, c) for c in codes),
                keywords={str(lang): tuple(str(w).lower() for w in words)
                          for lang, words in (p.get("keywords") or {}).items()},
                negative_keywords=tuple(str(w).lower() for w in p.get("negative_keywords") or []),
                osm_tags=tuple((str(k), str(v)) for k, v in p.get("osm_tags") or []),
            )
            self.entries[entry.id] = entry
            for words in (p.get("aliases") or {}).values():
                for word in words:
                    self._add(norm(str(word)), entry.id)
        for code, title in self.titles.items():          # bare ISIC entries (after profiles)
            eid = f"isic:{code}"
            if eid not in self.entries:
                self.entries[eid] = CatalogEntry(id=eid, isic=(code,), title=title,
                                                 keywords={"en": (title.lower(),)})
            self.index.setdefault(norm(title), self._profile_for_code(code) or eid)
        self.aliases = {k: v for k, v in (aliases.industries if aliases else {}).items()}

    def _add(self, key: str, entry_id: str) -> None:
        current = self.index.get(key)
        if current is not None and current != entry_id:
            self.collisions.append((key, current, entry_id))
            return
        self.index[key] = entry_id

    def _profile_for_code(self, code: str) -> str | None:
        """A curated profile whose ISIC set is exactly this one code (e.g."""
        for entry in self.entries.values():
            if entry.isic == (code,) and not entry.id.startswith("isic:"):
                return entry.id
        return None

    def _entry_for_code(self, code: str) -> str | None:
        code = code.upper() if code.isalpha() else code.zfill(2)
        if code not in self.titles:
            return None
        return self._profile_for_code(code) or f"isic:{code}"

    # --- resolution ------------------------------------------------------------------------------
    def resolve(self, text: str, languages: Sequence[str] = ("en",)) -> IndustryResolution:
        q = norm(text)
        m = _CODE_RE.match(q.replace(" ", "")) if q else None
        if m and (eid := self._entry_for_code(m.group(1))):
            return IndustryResolution(text, self._profile(eid, text, "catalog"))
        if q in self.index:
            return IndustryResolution(text, self._profile(self.index[q], text, "catalog"))
        if q in self.aliases:
            target = self.aliases[q]
            eid = target if target in self.entries else self._entry_for_code(target)
            if eid:
                return IndustryResolution(text, self._profile(eid, text, "alias"))
        key, _ = _pick(q, {k: k for k in self.index}) if q else (None, [])
        if key:
            eid = self.index[key]
            warning = (f"Industry '{text}' interpreted as "
                       f"'{self.entries[eid].title}' (fuzzy match)")
            return IndustryResolution(text, self._profile(eid, text, "fuzzy"), warning=warning)
        return IndustryResolution(text, None, self.suggest(q))

    def suggest(self, q: str, limit: int = SUGGESTION_LIMIT) -> tuple[dict[str, str], ...]:
        """Nearest ISIC titles."""
        choices = {code: norm(title) for code, title in self.titles.items()}
        choices.update({f"alias:{k}": k for k in self.index})
        out: list[dict[str, str]] = []
        for _, _, key in process.extract(q, choices, scorer=fuzz.WRatio, limit=limit * 4):
            codes = self.entries[self.index[key[6:]]].isic if key.startswith("alias:") else (key,)
            for code in codes:
                item = {"isic": code, "title": self.titles.get(code, code)}
                if item not in out:
                    out.append(item)
            if len(out) >= limit:
                break
        return tuple(out[:limit])

    def _profile(self, entry_id: str, text: str, method: str) -> IndustryProfile:
        e = self.entries[entry_id]
        return IndustryProfile(id=e.id, input=text, isic=e.isic, scheme=self.scheme,
                               version=self.version, keywords=dict(e.keywords),
                               negative_keywords=e.negative_keywords, osm_tags=e.osm_tags,
                               method=method, reviewed=True)

    # --- helpers for /meta and the resolved block ------------------------------------------------
    def search(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        """Autocomplete for ``GET /meta/industries?q=...``."""
        nq = norm(q)
        if not nq:
            return [{"id": e.id, "isic": list(e.isic), "title": e.title}
                    for e in list(self.entries.values())[:limit]]
        hits = process.extract(nq, {k: k for k in self.index}, scorer=fuzz.WRatio, limit=limit * 3)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, score, key in hits:
            eid = self.index[key]
            if eid in seen or score < C.INDUSTRY_SEARCH_MIN_SCORE:
                continue
            seen.add(eid)
            e = self.entries[eid]
            out.append({"id": e.id, "isic": list(e.isic), "title": e.title, "match": key})
            if len(out) >= limit:
                break
        return out


def keywords_for(profile: IndustryProfile, languages: Iterable[str]) -> dict[str, list[str]]:
    """Keywords restricted to the country's languages; falls back to English when the catalog has
    none of those languages."""
    out = {lang: list(profile.keywords[lang]) for lang in languages if lang in profile.keywords}
    if not out and "en" in profile.keywords:
        out["en"] = list(profile.keywords["en"])
    return out


def load_catalog(path: Path | str = CATALOG_FILE, aliases: Aliases | None = None) -> IndustryCatalog:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return IndustryCatalog(data, default_aliases() if aliases is None else aliases)


@lru_cache(maxsize=1)
def default_catalog() -> IndustryCatalog:
    return load_catalog()
