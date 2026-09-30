# src/leadscraper/sources/base.py
"""Source adapter Protocol."""

from collections.abc import AsyncIterator
from typing import Protocol

from leadscraper.domain.models import CompanyCandidate, SearchSlice


class SourceAdapter(Protocol):
    name: str
    countries: frozenset[str] | None   # None = global
    daily_budget: int | None           # per-day request limit, enforced in-process

    def discover(self, slice_: SearchSlice) -> AsyncIterator[CompanyCandidate]:
        """Async generator: yield candidates until the source is exhausted or the planner stops it."""
        ...
