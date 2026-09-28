# src/leadscraper/sources/base.py
"""Source adapter Protocol (ARCHITECTURE.md §4, code block verbatim).

Only change: the comment on ``daily_budget`` — A§4 says it is enforced by a Redis rate limiter;
in the zero-config v0.3 it is enforced in-process (PLAN.md Q2), never via Redis.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from leadscraper.domain.models import CompanyCandidate, SearchSlice


class SourceAdapter(Protocol):
    name: str
    countries: frozenset[str] | None   # None = global
    daily_budget: int | None           # per-day request limit, enforced in-process (Q2; not Redis)

    def discover(self, slice_: SearchSlice) -> AsyncIterator[CompanyCandidate]:
        """Async generator: yield candidates until the source is exhausted or the planner stops it."""
        ...
