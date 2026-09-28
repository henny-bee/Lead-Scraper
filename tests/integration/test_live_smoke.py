"""Opt-in live smoke test against the public Overpass instance (A§10.4). Skipped by default:
pytest runs with `-m "not live"` (pyproject addopts); run it explicitly with `pytest -m live`.
It sends exactly one small, paced query with the default User-Agent."""

import httpx
import pytest

from leadscraper.domain.models import SearchSlice
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.industry import default_catalog
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import OverpassAdapter, OverpassGate, SourceBudget

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_public_overpass_returns_candidates() -> None:
    settings = load_settings({})
    area = geo.resolve_region_detailed("DE", "Bremen").area
    industry = default_catalog().resolve("Logistik", ("de",)).profile
    async with httpx.AsyncClient(timeout=20) as client:        # adapter overrides per request
        adapter = OverpassAdapter(overpass_url=settings.overpass_url,
                                  user_agent=settings.crawler_user_agent, gate=OverpassGate(),
                                  budget=SourceBudget("osm", 1), areas={area.id: area},
                                  industries={industry.id: industry}, languages=("de",),
                                  client=client, retry_attempts=1, max_elements=50)
        s = SearchSlice("DE", area.id, industry.id, "Bremen", "Logistik", 50)
        candidates = [c async for c in adapter.discover(s)]
    outcome = adapter.outcomes[s]
    print(f"LIVE-RESULT candidates={len(candidates)} reason={outcome.reason} warnings={adapter.warnings}")
    if candidates:
        print("LIVE-SAMPLE", [(c.name, c.website, c.source_ref) for c in candidates[:3]])
    assert adapter.budget.used == 1
    assert candidates or adapter.warnings        # public service may be busy → warning, no crash
