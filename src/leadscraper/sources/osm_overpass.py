"""OpenStreetMap / Overpass discovery adapter (ARCHITECTURE.md §4, §3.4 "Discover", §10.4).

Politeness towards the public instance (A§1, A§4; Supervisor note T11):
- at most ``OVERPASS_MAX_CONCURRENCY`` (=1) request in flight per process and aiolimiter pacing
  (``OVERPASS_MAX_REQUESTS_PER_MINUTE``), shared through one :class:`OverpassGate` per process;
- an in-process daily cap (``daily_budget``, Q2) and a per-job request budget (:class:`SourceBudget`,
  ``OVERPASS_REQUEST_BUDGET_PER_JOB``) — every HTTP attempt counts and increments
  ``source_budget_used{source="osm"}``;
- 429/502/503/504/timeouts are retried with exponential backoff (tenacity); after that, or on an
  Overpass ``runtime error`` (e.g. query timeout), the slice is marked exhausted with a warning and
  the job continues. No quadtree ``sweep()`` in v0.3 (Q13).
- Output is bounded by the fixed cap ``OVERPASS_MAX_ELEMENTS_PER_QUERY`` (not the slice quota);
  a response reaching the cap is reported as ``saturated`` (``tiles_saturated_total``).
- ``User-Agent`` = ``CRAWLER_USER_AGENT``. No API key is read.

Query shape follows the A§4 example. Keywords are regex-escaped; keywords shorter than
``OVERPASS_SUBSTRING_MIN_LEN`` must match a whole word (``werk`` ≠ Handwerk/Werkstatt) and the
profile's negative keywords become a ``name!~`` filter plus a local name filter.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from aiolimiter import AsyncLimiter
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from leadscraper import constants as C
from leadscraper.domain.models import CompanyCandidate, GeoArea, IndustryProfile, SearchSlice
from leadscraper.observability import metrics
from leadscraper.observability.logging import get_logger
from leadscraper.services.resolver.industry import keywords_for

log = get_logger(__name__)

SOURCE_NAME = "osm"
OSM_AREA_OFFSET = 3_600_000_000              # area id = 3600000000 + relation id (A§4)
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_REGEX_SPECIAL = re.compile(r"([\\.^$|?*+()\[\]{}])")
_WORD_BOUNDARY_L = "(^|[^[:alnum:]])"
_WORD_BOUNDARY_R = "([^[:alnum:]]|$)"


# --- query building (pure) ----------------------------------------------------------------------
def ql_string(value: str) -> str:
    """Escape a value for use inside a double-quoted Overpass QL string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def regex_escape(keyword: str) -> str:
    """Escape POSIX-ERE metacharacters (the result still goes through :func:`ql_string`)."""
    return _REGEX_SPECIAL.sub(r"\\\1", keyword)


def keyword_pattern(keywords: Iterable[str], *, word_boundaries: bool = True,
                    min_substring_len: int = C.OVERPASS_SUBSTRING_MIN_LEN) -> str:
    parts: list[str] = []
    for kw in dict.fromkeys(k.strip().lower() for k in keywords if k and k.strip()):
        esc = regex_escape(kw)
        if word_boundaries and len(kw) < min_substring_len:
            esc = f"{_WORD_BOUNDARY_L}{esc}{_WORD_BOUNDARY_R}"
        parts.append(esc)
    return "|".join(parts)


def area_selector(area: GeoArea) -> str:
    if area.osm_relation_id is not None:
        return f"area(id:{OSM_AREA_OFFSET + area.osm_relation_id})->.region;"
    if area.level == "country":
        return f'area["ISO3166-1"="{ql_string(area.country_code)}"][admin_level=2]->.region;'
    if area.code:
        return f'area["ISO3166-2"="{ql_string(area.code)}"]->.region;'
    raise ValueError(f"area {area.id} has neither an ISO code nor an OSM relation id")


def slice_keywords(industry: IndustryProfile, languages: Sequence[str]) -> list[str]:
    """Local keywords of the country's languages (A§4: "industry keywords in the local language")."""
    return [w for words in keywords_for(industry, languages).values() for w in words]


def build_query(area: GeoArea, industry: IndustryProfile, languages: Sequence[str], *,
                limit: int | None = None, word_boundaries: bool = True,
                require_name: bool = True, named_set: bool = True,
                timeout_s: int = C.OVERPASS_QUERY_TIMEOUT_S) -> str:
    """Overpass QL for one slice, shaped like the A§4 ``Nordrhein-Westfalen × Produktion`` example.

    ``named_set`` (default, T25 live diagnosis): first collect the named elements of the area
    (``nwr(area.region)["name"]->.named``) and apply the name regex / tag filters to that set.
    Semantically identical to the A§4 form, but Overpass then evaluates the regex only on the
    area's elements instead of scanning every distinct ``name`` value worldwide. Measured against
    overpass-api.de for Bremen × Logistik: A§4 form → ``runtime error: Query timed out … after
    183 seconds``; named-set form → 50 elements in 12.6 s.
    """
    named = '["name"]' if require_name else ""
    lines = [f"[out:json][timeout:{timeout_s}];", area_selector(area)]
    if named_set:
        lines.append('nwr(area.region)["name"]->.named;')
        scope, suffix, named = "nwr.named", "", ""        # the set is already named + in area
    else:
        scope, suffix = "nwr", "(area.region)"
    lines.append("(")
    pattern = keyword_pattern(slice_keywords(industry, languages), word_boundaries=word_boundaries)
    if pattern:
        negative = keyword_pattern(industry.negative_keywords, word_boundaries=False)
        neg = f'["name"!~"{ql_string(negative)}",i]' if negative else ""
        lines.append(f'  {scope}["name"~"{ql_string(pattern)}",i]{neg}{suffix};')
    for key, value in industry.osm_tags:
        lines.append(f'  {scope}["{ql_string(key)}"="{ql_string(value)}"]{named}{suffix};')
    lines.append(");")
    lines.append(f"out tags center {limit};" if limit else "out tags center;")
    return "\n".join(lines)


# --- element mapping (pure) ---------------------------------------------------------------------
def element_to_candidate(el: dict[str, Any]) -> CompanyCandidate | None:
    tags: dict[str, str] = el.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name or el.get("type") not in ("node", "way", "relation") or "id" not in el:
        return None
    if el["type"] == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        center = el.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    website = (tags.get("website") or tags.get("contact:website") or "").strip() or None
    hints: dict[str, str] = {}
    for hint, keys in (("email", ("email", "contact:email")), ("phone", ("phone", "contact:phone")),
                       ("city", ("addr:city",))):
        value = next((tags[k].strip() for k in keys if tags.get(k, "").strip()), None)
        if value:
            hints[hint] = value
    return CompanyCandidate(
        name=name, source=SOURCE_NAME, source_ref=f"{el['type']}/{el['id']}", website=website,
        postal_code=(tags.get("addr:postcode") or "").strip() or None,
        lat=float(lat) if lat is not None else None, lon=float(lon) if lon is not None else None,
        coords_storable=True, hints=hints)


def is_excluded(name: str, negative_keywords: Iterable[str]) -> bool:
    low = name.lower()
    return any(n and n.lower() in low for n in negative_keywords)


# --- budgets and process-wide gate ---------------------------------------------------------------
@dataclass(slots=True)
class SourceBudget:
    """Per-job request budget for one source (job lifetime only)."""

    source: str
    limit: int
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def consume(self) -> bool:
        if self.exhausted:
            return False
        self.used += 1
        metrics.SOURCE_BUDGET_USED.labels(source=self.source).inc()
        return True


class OverpassGate:
    """Process-wide politeness: 1 request in flight, paced, daily cap (A§4 public limits)."""

    def __init__(self, *, concurrency: int = C.OVERPASS_MAX_CONCURRENCY,
                 per_minute: float = C.OVERPASS_MAX_REQUESTS_PER_MINUTE,
                 daily_budget: int | None = C.OVERPASS_DAILY_BUDGET) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)
        self.limiter = AsyncLimiter(1, 60 / per_minute)          # evenly spaced, no bursts
        self.daily_budget = daily_budget
        self._day = dt.date.today()
        self._used_today = 0
        self.in_flight = 0
        self.max_in_flight = 0

    def take_daily(self) -> bool:
        today = dt.date.today()
        if today != self._day:
            self._day, self._used_today = today, 0
        if self.daily_budget is not None and self._used_today >= self.daily_budget:
            return False
        self._used_today += 1
        return True


# --- adapter ------------------------------------------------------------------------------------
class _Retryable(Exception):
    pass


def _describe(exc: BaseException) -> str:
    """Short, data-free reason for warnings/logs, e.g. ``http 429`` or ``HTTPStatusError 400``."""
    if isinstance(exc, _Retryable):
        return str(exc)                                  # "http 504" / "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTPStatusError {exc.response.status_code}"
    return type(exc).__name__


class _BudgetExhausted(Exception):
    pass


@dataclass(slots=True)
class SliceOutcome:
    requests: int = 0
    candidates: int = 0
    exhausted: bool = False
    reason: str | None = None           # done | saturated | budget | daily_budget | error | runtime_error


@dataclass
class OverpassAdapter:
    """``SourceAdapter`` for OSM. One instance per job (holds the job's budget and outcomes)."""

    overpass_url: str
    user_agent: str
    gate: OverpassGate
    budget: SourceBudget
    areas: dict[str, GeoArea]
    industries: dict[str, IndustryProfile]
    languages: Sequence[str]
    client: httpx.AsyncClient | None = None
    retry_attempts: int = C.OVERPASS_RETRY_ATTEMPTS
    retry_wait_s: float = C.OVERPASS_RETRY_WAIT_S
    retry_wait_max_s: float = C.OVERPASS_RETRY_WAIT_MAX_S
    max_elements: int = C.OVERPASS_MAX_ELEMENTS_PER_QUERY
    name: str = SOURCE_NAME
    countries: frozenset[str] | None = None
    outcomes: dict[SearchSlice, SliceOutcome] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def daily_budget(self) -> int | None:
        return self.gate.daily_budget

    async def discover(self, slice_: SearchSlice) -> AsyncIterator[CompanyCandidate]:
        outcome = self.outcomes.setdefault(slice_, SliceOutcome())
        area = self.areas[slice_.area_id]
        industry = self.industries[slice_.industry_profile_id]
        query = build_query(area, industry, self.languages, limit=self.max_elements)
        try:
            data = await self._post(query, outcome)
        except _BudgetExhausted as exc:
            self._exhaust(slice_, outcome, str(exc),
                          f"Overpass request budget exhausted for slice {slice_.region_label} × "
                          f"{slice_.industry_label}; results may be fewer than max_output.")
            return
        except (_Retryable, httpx.HTTPError, ValueError) as exc:
            self._exhaust(slice_, outcome, "error",
                          f"Overpass unavailable for slice {slice_.region_label} × "
                          f"{slice_.industry_label} ({_describe(exc)}); slice skipped.")
            return
        remark = str(data.get("remark") or "")
        if "runtime error" in remark.lower():
            self._exhaust(slice_, outcome, "runtime_error",
                          f"Overpass query for slice {slice_.region_label} × "
                          f"{slice_.industry_label} failed/timed out on the server; slice skipped.")
            # partial elements (if any) are still usable
        elements = data.get("elements") or []
        if len(elements) >= self.max_elements:        # truncated at the cap, not exhausted
            metrics.TILES_SATURATED.labels(source=self.name).inc()
            self._exhaust(slice_, outcome, "saturated",
                          f"Overpass results for slice {slice_.region_label} × "
                          f"{slice_.industry_label} truncated at {self.max_elements} elements; "
                          f"some candidates may be missed (no tiling in v0.3).")
        seen: set[str] = set()
        for el in elements:
            cand = element_to_candidate(el)
            if cand is None or cand.source_ref in seen:
                continue
            if is_excluded(cand.name, industry.negative_keywords):
                continue
            seen.add(cand.source_ref)
            outcome.candidates += 1
            metrics.CANDIDATES_DISCOVERED.labels(country=slice_.country_code, source=self.name).inc()
            yield cand
        outcome.exhausted = True                  # one query per slice in v0.3 (no sweep, Q13)
        outcome.reason = outcome.reason or "done"

    def _exhaust(self, slice_: SearchSlice, outcome: SliceOutcome, reason: str, warning: str) -> None:
        outcome.exhausted, outcome.reason = True, reason
        if warning not in self.warnings:
            self.warnings.append(warning)
        log.warning("overpass_slice_exhausted", reason=reason, area=slice_.area_id,
                    industry=slice_.industry_profile_id, detail=warning)

    async def _post(self, query: str, outcome: SliceOutcome) -> dict[str, Any]:
        client = self.client or httpx.AsyncClient(timeout=C.OVERPASS_HTTP_TIMEOUT_S)
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.retry_attempts),
                wait=wait_exponential(multiplier=self.retry_wait_s, max=self.retry_wait_max_s),
                retry=retry_if_exception_type(_Retryable), reraise=True,
            ):
                with attempt:
                    return await self._request(client, query, outcome)
        finally:
            if self.client is None:
                await client.aclose()
        raise _Retryable("unreachable")  # pragma: no cover

    async def _request(self, client: httpx.AsyncClient, query: str,
                       outcome: SliceOutcome) -> dict[str, Any]:
        if self.budget.exhausted:
            raise _BudgetExhausted("budget")
        if not self.gate.take_daily():
            raise _BudgetExhausted("daily_budget")
        self.budget.consume()
        outcome.requests += 1
        async with self.gate.semaphore:
            self.gate.in_flight += 1
            self.gate.max_in_flight = max(self.gate.max_in_flight, self.gate.in_flight)
            try:
                async with self.gate.limiter:
                    resp = await client.post(self.overpass_url, data={"data": query},
                                             headers={"User-Agent": self.user_agent},
                                             timeout=C.OVERPASS_HTTP_TIMEOUT_S)  # > [timeout:N]
            except httpx.TimeoutException as exc:
                raise _Retryable("timeout") from exc
            finally:
                self.gate.in_flight -= 1
        if resp.status_code in RETRYABLE_STATUS:
            raise _Retryable(f"http {resp.status_code}")
        resp.raise_for_status()
        return resp.json()
