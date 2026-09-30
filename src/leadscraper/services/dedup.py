"""Deduplication with the overlap rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from leadscraper import constants as C
from leadscraper.crawler.website import registered_domain
from leadscraper.domain.models import CompanyCandidate, SearchSlice
from leadscraper.services.planner import Plan, area_specificity, industry_specificity
from leadscraper.services.resolver.geo import norm

LEGAL_FORMS_FILE = C.CONFIG_DIR / "i18n" / "legal_forms.yaml"
_NON_WORD = re.compile(r"[^\w\s]")


@lru_cache(maxsize=1)
def _legal_forms_file() -> dict:
    p = Path(LEGAL_FORMS_FILE)
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.is_file() else {}


def legal_form_tokens(cc: str | None) -> list[tuple[str, ...]]:
    """Normalised legal-form token sequences for a country (longest first)."""
    data = _legal_forms_file()
    forms: list[str] = []
    for key in ([cc] if cc else list(data)):
        entry = data.get(key) or {}
        forms += [str(f) for f in (entry.get("suffix") or []) + (entry.get("prefix") or [])]
    seqs = {tuple(_NON_WORD.sub(" ", norm(f)).split()) for f in forms}
    return sorted((s for s in seqs if s), key=len, reverse=True)


def normalize_name(name: str, forms: list[tuple[str, ...]]) -> str:
    tokens = _NON_WORD.sub(" ", norm(name)).split()
    changed = True
    while changed:
        changed = False
        for seq in forms:
            n = len(seq)
            for i in range(len(tokens) - n + 1):
                if tuple(tokens[i:i + n]) == seq:
                    del tokens[i:i + n]
                    changed = True
                    break
    return " ".join(tokens)


def normalize_postal(code: str | None) -> str | None:
    return re.sub(r"\s+", "", code).upper() if code and code.strip() else None


@dataclass(slots=True)
class DedupEntry:
    candidate: CompanyCandidate
    slice: SearchSlice                      # assignment used for region/industry labels
    domain: str | None
    norm_name: str
    postal: str | None
    found_in: list[SearchSlice] = field(default_factory=list)

    @property
    def region_label(self) -> str:
        return self.slice.region_label

    @property
    def industry_label(self) -> str:
        return self.slice.industry_label


@dataclass(slots=True)
class DedupResult:
    entry: DedupEntry
    new: bool
    reassigned: bool = False


class Deduplicator:
    def __init__(self, plan: Plan, threshold: float = C.DEDUP_NAME_THRESHOLD) -> None:
        self.plan = plan
        self.threshold = threshold
        self.forms = legal_form_tokens(plan.country_code)
        self.entries: list[DedupEntry] = []
        self._by_domain: dict[str, DedupEntry] = {}
        self._by_ref: dict[str, DedupEntry] = {}
        self._by_postal: dict[str, list[DedupEntry]] = {}

    def add(self, cand: CompanyCandidate, slice_: SearchSlice) -> DedupResult:
        domain = registered_domain(cand.website)
        postal = normalize_postal(cand.postal_code)
        name = normalize_name(cand.name, self.forms)
        ref = f"{cand.source}:{cand.source_ref}"
        existing = (self._by_ref.get(ref)
                    or (self._by_domain.get(domain) if domain else None)
                    or self._match_name(name, postal, domain))
        if existing is None:
            entry = DedupEntry(cand, slice_, domain, name, postal, [slice_])
            self.entries.append(entry)
            self._index(entry, ref)
            return DedupResult(entry, new=True)
        existing.found_in.append(slice_)
        reassigned = self._reassign(existing, slice_)
        self._merge(existing, cand, domain)
        self._index(existing, ref)
        return DedupResult(existing, new=False, reassigned=reassigned)

    def known_domain(self, domain: str | None) -> bool:
        """Step 6a: the registered domain belongs to an entry already (the OSM data wins)."""
        return bool(domain) and domain in self._by_domain

    def match_name(self, name: str, postal_code: str | None, *, source: str | None = None,
                   area_id: str | None = None) -> DedupEntry | None:
        """Step 6b: the name rule (normalised name without legal forms, ``token_set_ratio ≥
        threshold``, the same postcode required) for a name found after the crawl, restricted to
        entries of ``source`` in ``area_id``."""
        postal = normalize_postal(postal_code)
        norm_name = normalize_name(name, self.forms)
        if not postal or not norm_name:
            return None
        for other in self._by_postal.get(postal, []):
            if source is not None and other.candidate.source != source:
                continue
            if area_id is not None and other.slice.area_id != area_id:
                continue
            if fuzz.token_set_ratio(norm_name, other.norm_name) >= self.threshold:
                return other
        return None

    def _match_name(self, name: str, postal: str | None, domain: str | None) -> DedupEntry | None:
        if not postal or not name:
            return None
        for other in self._by_postal.get(postal, []):
            if domain and other.domain and other.domain != domain:
                continue                          # different websites → different companies
            if fuzz.token_set_ratio(name, other.norm_name) >= self.threshold:
                return other
        return None

    def _index(self, entry: DedupEntry, ref: str) -> None:
        self._by_ref[ref] = entry
        if entry.domain:
            self._by_domain.setdefault(entry.domain, entry)
        if entry.postal and entry not in self._by_postal.setdefault(entry.postal, []):
            self._by_postal[entry.postal].append(entry)

    def _reassign(self, entry: DedupEntry, new: SearchSlice) -> bool:
        cur = entry.slice
        ind = self.plan.industries
        area = self.plan.areas
        best_ind = cur
        if industry_specificity(ind[new.industry_profile_id]) > \
                industry_specificity(ind[cur.industry_profile_id]):
            best_ind = new
        best_area = cur
        if area_specificity(area[new.area_id]) > area_specificity(area[cur.area_id]):
            best_area = new
        if best_ind is cur and best_area is cur:
            return False
        entry.slice = SearchSlice(
            country_code=cur.country_code, area_id=best_area.area_id,
            industry_profile_id=best_ind.industry_profile_id, region_label=best_area.region_label,
            industry_label=best_ind.industry_label, quota=cur.quota)
        return True

    @staticmethod
    def _merge(entry: DedupEntry, cand: CompanyCandidate, domain: str | None) -> None:
        c = entry.candidate
        if not c.website and cand.website:
            c.website, entry.domain = cand.website, domain
        c.postal_code = c.postal_code or cand.postal_code
        if c.lat is None and cand.lat is not None:
            c.lat, c.lon = cand.lat, cand.lon
        for k, v in cand.hints.items():
            c.hints.setdefault(k, v)
