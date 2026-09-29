"""Planner: slices area × industry → quota per slice."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import pycountry

from leadscraper import constants as C
from leadscraper.domain.models import GeoArea, IndustryProfile, SearchSlice
from leadscraper.domain.quota import allocate
from leadscraper.services.resolver.resolve import ResolvedRequest


def slice_key(s: SearchSlice) -> str:
    return f"{s.area_id}|{s.industry_profile_id}"


def overfetch_target(max_output: int, tier: str) -> int:
    factor = C.OVERFETCH_FACTOR_BY_TIER.get(tier, C.OVERFETCH_FACTOR_DEFAULT)
    return max(max_output, math.ceil(max_output * factor))


def industry_specificity(profile: IndustryProfile) -> tuple[int, int]:
    """Higher = more specific: divisions (2 digits) beat sections (letters); fewer codes beat more."""
    level = max((2 if code.isdigit() else 1) for code in profile.isic) if profile.isic else 0
    return level, -len(profile.isic)


def area_specificity(area: GeoArea) -> int:
    """Higher = smaller area: country 0, ISO subdivision 1 + number of ISO ancestors, Nominatim/OSM
    relation 3."""
    if area.level == "country":
        return 0
    if area.osm_relation_id is not None or not area.code:
        return 3
    depth, code = 1, area.code
    for _ in range(5):
        sub = pycountry.subdivisions.get(code=code)
        parent = getattr(sub, "parent_code", None) if sub else None
        if not parent:
            break
        depth, code = depth + 1, parent
    return depth


@dataclass(slots=True)
class SlicePlan:
    slice: SearchSlice
    order: int
    capacity: int
    produced: int = 0
    exhausted: bool = False


@dataclass
class Plan:
    """Per-job plan (job lifetime only)."""

    country_code: str
    target: int
    slices: dict[str, SlicePlan] = field(default_factory=dict)
    areas: dict[str, GeoArea] = field(default_factory=dict)
    industries: dict[str, IndustryProfile] = field(default_factory=dict)

    def ordered(self) -> list[SearchSlice]:
        return [p.slice for p in sorted(self.slices.values(), key=lambda p: p.order)]

    def quota(self, key: str) -> int:
        return self.slices[key].slice.quota

    def record(self, key: str, n: int = 1) -> None:
        self.slices[key].produced += n

    def mark_exhausted(self, key: str) -> dict[str, int]:
        """Lock the slice at what it produced and re-allocate the remaining target."""
        sp = self.slices[key]
        sp.exhausted = True
        sp.capacity = sp.produced
        return self.rebalance()

    def rebalance(self) -> dict[str, int]:
        capacity = {k: (p.capacity if p.exhausted else max(p.capacity, self.target))
                    for k, p in self.slices.items()}
        alloc = allocate(self.target, capacity)
        for k, p in self.slices.items():
            p.slice = replace(p.slice, quota=alloc[k])
        return alloc

    @property
    def active(self) -> list[SearchSlice]:
        return [p.slice for p in sorted(self.slices.values(), key=lambda p: p.order)
                if not p.exhausted and p.slice.quota > p.produced]


def country_split(area: GeoArea) -> list[GeoArea]:
    """The first-level subdivisions of a country-level area; ``[]`` = keep the single country slice
    (none, or > the cap)."""
    if area.level != "country":
        return []
    subs = [s for s in pycountry.subdivisions.get(country_code=area.country_code) or ()
            if not getattr(s, "parent_code", None) and s.code not in C.COUNTRY_SPLIT_EXCLUDED_CODES]
    if not 1 <= len(subs) <= C.COUNTRY_SPLIT_MAX_SUBDIVISIONS:
        return []
    return [GeoArea(id=f"iso:{s.code}", country_code=area.country_code, name=s.name, level=s.type,
                    code=s.code, input="", method="country") for s in sorted(subs, key=lambda s: s.code)]


def build_plan(resolved: ResolvedRequest) -> Plan:
    req = resolved.request
    target = overfetch_target(req.max_output, resolved.profile.tier)
    plan = Plan(country_code=resolved.country_code, target=target)
    order = 0
    for requested in resolved.regions:
        for area in country_split(requested) or [requested]:
            plan.areas[area.id] = area
            for industry in resolved.industries:
                plan.industries.setdefault(industry.id, industry)
                s = SearchSlice(country_code=resolved.country_code, area_id=area.id,
                                industry_profile_id=industry.id,
                                region_label=requested.input,   # "" when regions is empty (whole country)
                                industry_label=industry.input, quota=0)
                key = slice_key(s)
                if key in plan.slices:                # e.g. "Logistik" + "Spedition" → same profile
                    continue
                plan.slices[key] = SlicePlan(slice=s, order=order, capacity=target)
                order += 1
    plan.rebalance()
    return plan
