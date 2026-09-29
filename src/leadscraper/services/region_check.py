"""Source-agnostic region check."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from leadscraper import constants as C
from leadscraper.domain.models import CompanyCandidate, GeoArea
from leadscraper.extractors.address import own_address
from leadscraper.extractors.identity import translit

RegionConfidence = Literal["high", "low"]


@dataclass(slots=True, frozen=True)
class Evidence:
    """The company's own address as found on its website."""

    postcode: str | None
    city: str | None


@dataclass(slots=True, frozen=True)
class AreaGazetteer:
    """Postcodes (normalised:func:`normalise_postcode`) and place names (:func:`normalise_place`) of
    one area, from one Overpass query per area and job."""

    postcodes: frozenset[str] = frozenset()
    places: frozenset[str] = frozenset()


def normalise_postcode(code: str) -> str:
    """``"sw1a 1aa"`` → ``"SW1A1AA"`` (spaces removed, upper case)."""
    return re.sub(r"\s+", "", code).upper()


def normalise_place(name: str) -> str:
    """``"Düsseldorf"`` → ``"duesseldorf"`` (the identity check's transliteration)."""
    return " ".join(translit(name).split())


def needs_evidence(candidate: CompanyCandidate) -> bool:
    """False when rule 1 or 2 already decides (no address evidence or gazetteer is needed)."""
    return candidate.source != "osm" and candidate.hints.get("area_match") != "source"


def company_evidence(legal_page_texts: Sequence[str], jsonld_orgs: Sequence[object],
                     patterns: Sequence[re.Pattern], cc: str) -> Evidence | None:
    """Rule-3 evidence via:func:`leadscraper.extractors.address.own_address`."""
    own = own_address(legal_page_texts, jsonld_orgs, patterns, cc)
    return Evidence(*own) if own is not None else None


def is_city_level(area: GeoArea) -> bool:
    """An OSM-relation area (Nominatim) or one whose level is a place type (``city``, ``town``, …)."""
    return area.osm_relation_id is not None or area.level.lower() in C.GAZETTEER_PLACE_TYPES


def region_confidence(candidate: CompanyCandidate, *, area: GeoArea | None,
                      evidence: Evidence | None, gazetteer: AreaGazetteer | None,
                      osm_postcodes: Iterable[str]) -> RegionConfidence:
    if candidate.source == "osm":                                             # rule 1
        return "high"
    if candidate.hints.get("area_match") == "source":                         # rule 2
        return "high"
    if area is None or evidence is None:                                      # rule 4
        return "low"
    known = {normalise_postcode(p) for p in osm_postcodes if p and p.strip()}
    if gazetteer is not None:
        known |= gazetteer.postcodes
    if evidence.postcode and evidence.postcode.strip() and known:            # rule 3, postcode
        return "high" if normalise_postcode(evidence.postcode) in known else "low"
    city = normalise_place(evidence.city or "")                               # rule 3, city
    if not city:
        return "low"
    if gazetteer is not None and city in gazetteer.places:
        return "high"
    if is_city_level(area) and city == normalise_place(area.name):
        return "high"
    return "low"
