"""Source adapter registry: only *enabled* adapters (PLAN.md Q8, T11).

v0.3 ships exactly one adapter, ``osm`` (Overpass). ``GOOGLE_PLACES_ENABLED`` /
``COMPANIES_HOUSE_ENABLED`` refer to optional adapters planned for v1.0 (A§12, Q18); they are not
implemented, so setting them only logs a warning and never adds a source. No API key is read here.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from leadscraper import constants as C
from leadscraper.domain.models import GeoArea, IndustryProfile
from leadscraper.observability.logging import get_logger
from leadscraper.services.resolver.profile import CountryProfile
from leadscraper.settings import Settings
from leadscraper.sources.base import SourceAdapter
from leadscraper.sources.osm_overpass import SOURCE_NAME, OverpassAdapter, OverpassGate, SourceBudget

log = get_logger(__name__)

AVAILABLE_SOURCES: tuple[str, ...] = (SOURCE_NAME,)


def enabled_source_names(settings: Settings) -> tuple[str, ...]:
    if settings.google_places_enabled or settings.companies_house_enabled:
        log.warning("optional_adapter_not_available",
                    detail="Google Places / Companies House adapters are not part of v0.3")
    return AVAILABLE_SOURCES


def build_adapters(settings: Settings, profile: CountryProfile, *, gate: OverpassGate,
                   areas: dict[str, GeoArea], industries: dict[str, IndustryProfile],
                   client: httpx.AsyncClient | None = None,
                   languages: Sequence[str] | None = None) -> list[SourceAdapter]:
    """Per-job adapter instances for the sources listed in ``profile.sources`` that are enabled."""
    enabled = set(enabled_source_names(settings))
    adapters: list[SourceAdapter] = []
    for name in profile.sources:
        if name not in enabled:
            continue
        if name == SOURCE_NAME:
            adapters.append(OverpassAdapter(
                overpass_url=settings.overpass_url, user_agent=settings.crawler_user_agent,
                gate=gate, budget=SourceBudget(SOURCE_NAME, C.OVERPASS_REQUEST_BUDGET_PER_JOB),
                areas=areas, industries=industries,
                languages=tuple(languages or profile.languages), client=client))
    return adapters
