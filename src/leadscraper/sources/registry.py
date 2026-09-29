"""Source adapter registry: only *enabled* adapters."""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from leadscraper import constants as C
from leadscraper.domain.models import GeoArea, IndustryProfile
from leadscraper.observability.logging import get_logger
from leadscraper.services.resolver.profile import CountryProfile
from leadscraper.settings import Settings
from leadscraper.sources.base import SourceAdapter
from leadscraper.sources.osm_overpass import (
    SOURCE_NAME,
    OverpassAdapter,
    OverpassGate,
    OverpassGatePool,
    SourceBudget,
)
from leadscraper.sources.web_search import SOURCE_NAME as WEB_SEARCH
from leadscraper.sources.web_search import SearchBackend, SearchGate, SearchJob, WebSearchAdapter

log = get_logger(__name__)

AVAILABLE_SOURCES: tuple[str, ...] = (SOURCE_NAME, WEB_SEARCH)


def enabled_source_names(settings: Settings) -> tuple[str, ...]:
    if settings.google_places_enabled or settings.companies_house_enabled:
        log.warning("optional_adapter_not_available",
                    detail="Google Places / Companies House adapters are not part of v0.3")
    return tuple(s for s in AVAILABLE_SOURCES if s != WEB_SEARCH or settings.web_search_enabled)


def build_adapters(settings: Settings, profile: CountryProfile, *, gate: OverpassGate | OverpassGatePool,
                   areas: dict[str, GeoArea], industries: dict[str, IndustryProfile],
                   client: httpx.AsyncClient | None = None,
                   languages: Sequence[str] | None = None,
                   search_backend: SearchBackend | None = None, search_gate: SearchGate | None = None,
                   search_job: SearchJob | None = None) -> list[SourceAdapter]:
    """Per-job adapter instances for the sources listed in ``profile.sources`` that are enabled
    (``web_search`` only with a search backend and gate)."""
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
        elif name == WEB_SEARCH and search_backend is not None and search_gate is not None:
            adapters.append(WebSearchAdapter(
                backend=search_backend, gate=search_gate, job=search_job or SearchJob(),
                areas=areas, industries=industries,
                languages=tuple(languages or profile.languages), country=profile.code))
    return adapters
