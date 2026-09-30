"""Pure domain models (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class JobStatus(StrEnum):
    """Job lifecycle states: queued → running → success | failed | cancelled."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED)


@dataclass(slots=True, frozen=True)
class SearchSlice:
    country_code: str          # "DE", "FR", ...
    area_id: str               # "iso:DE-BY" | "gn:<geonameId>" | "osm:r<relationId>"
    industry_profile_id: str
    region_label: str          # exact label from the request, echoed in the response
    industry_label: str
    quota: int


@dataclass(slots=True)
class CompanyCandidate:
    name: str
    source: str                # "google_places" | "osm" | "gb_companies_house" | ...
    source_ref: str            # place_id, OSM element id, register number, ...
    website: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    coords_storable: bool = False   # True for OSM/register; False for Google Places
    hints: dict[str, str] = field(default_factory=dict)  # e.g. email from an OSM tag


@dataclass(slots=True, frozen=True)
class GeoArea:
    """Resolved region."""

    id: str                                    # "iso:DE-BY" | "gn:<geonameId>" | "osm:r<relationId>"
    country_code: str
    name: str
    level: str                                 # pycountry subdivision type, e.g. "state", "province"
    input: str = ""                            # label exactly as sent by the client
    method: str = "exact"                      # exact | alias | fuzzy | nominatim | country
    code: str | None = None                    # ISO 3166-2 code, when the area has one
    osm_relation_id: int | None = None         # Overpass `area(id:3600000000+rel)`
    bbox: tuple[float, float, float, float] | None = None   # (south, west, north, east)


@dataclass(slots=True, frozen=True)
class IndustryProfile:
    """Resolved industry."""

    id: str
    input: str                                 # label exactly as sent by the client
    isic: tuple[str, ...]                      # ISIC codes: section letters and/or divisions
    scheme: str = "ISIC"
    version: str = "Rev.4"
    keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)   # language -> keywords
    negative_keywords: tuple[str, ...] = ()
    osm_tags: tuple[tuple[str, str], ...] = ()  # OSM filter hints, e.g. (("man_made", "works"),)
    method: str = "catalog"                    # catalog | alias | fuzzy
    reviewed: bool = True


@dataclass(slots=True)
class EmailFinding:
    """One email found on a website, kept per job with provenance."""

    email: str
    source_url: str
    on_legal_notice: bool = False
    from_jsonld: bool = False


@dataclass(slots=True)
class CompanyRecord:
    """A company assembled during one job."""

    name: str
    source: str
    source_ref: str
    country_label: str                         # echo labels
    region_label: str
    industry_label: str
    area_id: str = ""
    industry_profile_id: str = ""
    website: str | None = None
    registered_domain: str | None = None
    legal_name: str | None = None
    legal_form: str | None = None
    register_number: str | None = None
    vat_id: str | None = None
    phone: str | None = None
    address: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    emails: list[EmailFinding] = field(default_factory=list)
    company_email: str | None = None
    marketing_objection: bool = False
    region_confidence: str = "high"            # "low" -> excluded by default
