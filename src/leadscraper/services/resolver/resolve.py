"""Full request resolution → the ``resolved`` block (ARCHITECTURE.md §2.2, §3.2, §3.4 "Resolve").

Unknown/ambiguous input raises :class:`ResolutionError` (mapped to the A§2.3 ``422`` envelope with
codes ``unresolved_country`` / ``unresolved_region`` / ``unresolved_industry``). Fuzzy corrections,
tier notes and possibly unavailable ``information`` fields become warnings. No network is used
unless ``NOMINATIM_URL`` is set (Q7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import pycountry
import yaml
from stdnum.util import get_cc_module

from leadscraper import constants as C
from leadscraper.domain.models import GeoArea, IndustryProfile
from leadscraper.observability import metrics
from leadscraper.schemas.scrape import InformationField, ScrapeRequest
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.industry import IndustryCatalog, default_catalog, keywords_for
from leadscraper.services.resolver.profile import EU_EEA, OPEN_REGISTERS, CountryProfile, ProfileBuilder
from leadscraper.settings import Settings

LEGAL_FORMS_FILE = C.CONFIG_DIR / "i18n" / "legal_forms.yaml"

TIER_C_WARNING = ("Tier C: business websites are not required to publish a legal notice, so the email yield "
                  "is expected to be lower.")                                      # A§2.2 example
TIER_A_REGISTER_WARNING = ("Tier A: the open company register '{adapter}' is available for {cc}, "
                           "but its adapter is not enabled in this deployment; discovery uses "
                           "{sources}.")
NOMINATIM_HINT = ("City/district-level regions require NOMINATIM_URL (not set); use the ISO 3166-2 "
                  "province/state name or an alias.")


class ResolutionError(Exception):
    """Input that cannot be resolved confidently → ``422`` (never guessed, C13)."""

    def __init__(self, code: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


@dataclass(slots=True)
class ResolvedRequest:
    request: ScrapeRequest
    country: geo.CountryResolution
    profile: CountryProfile
    regions: list[GeoArea]                  # one per input region, or the whole country
    industries: list[IndustryProfile]
    warnings: list[str] = field(default_factory=list)

    @property
    def country_code(self) -> str:
        return self.profile.code

    def to_block(self, known_in_job: int = 0) -> dict[str, Any]:
        """The A§2.2 ``resolved`` object (no ``known_in_db``; ``known_in_job`` per Q24)."""
        langs = self.profile.languages
        return {
            "country": {"input": self.country.input, "code": self.profile.code,
                        "languages": list(langs), "tier": self.profile.tier},
            "regions": [
                {"input": a.input or None, "id": a.id, "name": a.name, "level": a.level,
                 "method": a.method}
                for a in self.regions
            ],
            "industries": [
                {"input": p.input, "isic": list(p.isic), "scheme": p.scheme, "version": p.version,
                 "keywords": keywords_for(p, langs), "method": p.method}
                for p in self.industries
            ],
            "sources": list(self.profile.sources),
            "known_in_job": known_in_job,
            "compliance_note": compliance_note(self.profile),
            "warnings": list(self.warnings),
        }


def compliance_note(profile: CountryProfile) -> str:
    """Override YAML ``compliance_note`` (seeded DE/FR/GB), else a generic note (T10 note)."""
    note = profile.overrides.get("compliance_note")
    if note:
        return str(note)
    if profile.code in EU_EEA | {"CH"}:
        return (f"{profile.code}: GDPR (or equivalent data protection law) applies to personal "
                f"data. Check outreach rules before using the data for marketing.")
    return (f"{profile.code}: Check local data protection and outreach rules before using the data "
            f"for marketing.")


@lru_cache(maxsize=1)
def _legal_forms() -> dict[str, Any]:
    p = Path(LEGAL_FORMS_FILE)
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.is_file() else {}


def unavailable_fields(cc: str, information: list[InformationField]) -> list[InformationField]:
    """Fields that probably cannot be extracted/validated for this country (warning only)."""
    forms = _legal_forms().get(cc, {}) or {}
    out: list[InformationField] = []
    for f in information:
        if f is InformationField.LEGAL_FORM and not (forms.get("suffix") or forms.get("prefix")):
            out.append(f)
        elif f is InformationField.REGISTER_NUMBER and not forms.get("register"):
            out.append(f)
        elif f is InformationField.VAT_ID and get_cc_module(cc.lower(), "vat") is None:
            out.append(f)
    return out


class Resolver:
    def __init__(self, settings: Settings, *, profiles: ProfileBuilder | None = None,
                 catalog: IndustryCatalog | None = None, aliases: geo.Aliases | None = None,
                 nominatim: geo.NominatimClient | None = None) -> None:
        self.settings = settings
        self.profiles = profiles or ProfileBuilder()
        self.catalog = catalog or default_catalog()
        self.aliases = aliases if aliases is not None else geo.default_aliases()
        if nominatim is None and settings.nominatim_url:
            nominatim = geo.NominatimClient(settings.nominatim_url,
                                            max_rps=settings.nominatim_max_rps,
                                            user_agent=settings.crawler_user_agent)
        self.nominatim = nominatim

    def resolve_country(self, text: str) -> geo.CountryResolution:
        res = geo.resolve_country_detailed(text, self.aliases)
        metrics.RESOLVER_RESULTS.labels(field="country", method=res.method or "failed").inc()
        if res.code is None:
            raise ResolutionError(
                "unresolved_country", f"Country '{text}' could not be resolved",
                {"input": text, "suggestions": [_country_suggestion(c) for c in res.suggestions]})
        return res

    async def resolve(self, req: ScrapeRequest) -> ResolvedRequest:
        country = self.resolve_country(req.country)
        cc = country.code
        assert cc is not None
        profile = self.profiles.get(cc)
        warnings: list[str] = []
        if country.method == "fuzzy":
            warnings.append(f"Country '{req.country}' corrected to {cc} (fuzzy match)")

        regions: list[GeoArea] = []
        for text in req.regions:
            area = await self._resolve_region(cc, text)
            if area.method == "fuzzy":
                warnings.append(f"Region '{text}' corrected to '{area.name}' (fuzzy match)")
            regions.append(area)
        if not req.regions:
            regions.append(geo.country_area(cc))

        industries: list[IndustryProfile] = []
        for text in req.industries:
            res = self.catalog.resolve(text, profile.languages)
            metrics.RESOLVER_RESULTS.labels(field="industry", method=res.method or "failed").inc()
            if res.profile is None:
                raise ResolutionError(
                    "unresolved_industry", f"Industry '{text}' could not be resolved",
                    {"input": text, "suggestions": list(res.suggestions)})
            if res.warning:
                warnings.append(res.warning)
            industries.append(res.profile)

        warnings.extend(_tier_warnings(profile))
        for f in unavailable_fields(cc, req.information):
            warnings.append(f"Field '{f.value}' may not be available for {cc}; its value is "
                            f"returned as null when not found.")
        return ResolvedRequest(req, country, profile, regions, industries, warnings)

    async def _resolve_region(self, cc: str, text: str) -> GeoArea:
        res = geo.resolve_region_detailed(cc, text, self.aliases)
        area = res.area
        if area is None and self.nominatim is not None:
            area = await self.nominatim.lookup(cc, text)
        metrics.RESOLVER_RESULTS.labels(field="region",
                                        method=area.method if area else "failed").inc()
        if area is None:
            details: dict[str, Any] = {"input": text,
                                       "suggestions": [geo.region_suggestion(c) for c in res.suggestions]}
            if not res.suggestions and self.nominatim is None:
                details["hint"] = NOMINATIM_HINT
            raise ResolutionError("unresolved_region",
                                  f"Region '{text}' could not be resolved for country {cc}", details)
        return area


def _country_suggestion(code: str) -> dict[str, str]:
    c = pycountry.countries.get(alpha_2=code)
    return {"code": code, "name": c.name if c else code}


def _tier_warnings(profile: CountryProfile) -> list[str]:
    if profile.tier == "C":
        return [TIER_C_WARNING]
    if profile.tier == "A":
        adapter = OPEN_REGISTERS.get(profile.code)
        if adapter and adapter not in profile.sources:
            return [TIER_A_REGISTER_WARNING.format(adapter=adapter, cc=profile.code,
                                                   sources=", ".join(profile.sources) or "-")]
    return []
