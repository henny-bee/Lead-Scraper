"""OpenStreetMap / Overpass discovery adapter."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from aiolimiter import AsyncLimiter
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from leadscraper import constants as C
from leadscraper.domain.models import CompanyCandidate, GeoArea, IndustryProfile, SearchSlice
from leadscraper.observability import metrics
from leadscraper.observability.logging import get_logger
from leadscraper.services.region_check import AreaGazetteer, normalise_place, normalise_postcode
from leadscraper.services.resolver.industry import keywords_for
from leadscraper.settings import Settings

log = get_logger(__name__)

SOURCE_NAME = "osm"
DEFAULT_OVERPASS_URL: str = Settings.model_fields["overpass_url"].default   # the public instance
OSM_AREA_OFFSET = 3_600_000_000              # area id = 3600000000 + relation id
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_REGEX_SPECIAL = re.compile(r"([\\.^$|?*+()\[\]{}])")
_WORD_BOUNDARY_L = "(^|[^[:alnum:]])"
_WORD_BOUNDARY_R = "([^[:alnum:]]|$)"


# --- query building (pure) ----------------------------------------------------------------------
def ql_string(value: str) -> str:
    """Escape a value for use inside a double-quoted Overpass QL string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def regex_escape(keyword: str) -> str:
    """Escape POSIX-ERE metacharacters (the result still goes through:func:`ql_string`)."""
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
    """Local keywords of the country's languages."""
    return [w for words in keywords_for(industry, languages).values() for w in words]


def build_query(area: GeoArea, industry: IndustryProfile, languages: Sequence[str], *,
                limit: int | None = None, word_boundaries: bool = True,
                require_name: bool = True, named_set: bool = True,
                timeout_s: int = C.OVERPASS_QUERY_TIMEOUT_S) -> str:
    """Overpass QL for one slice, shaped like the ``Nordrhein-Westfalen × Produktion`` example."""
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
        test = f'["{ql_string(key)}"]' if value == "*" else f'["{ql_string(key)}"="{ql_string(value)}"]'
        lines.append(f'  {scope}{test}{named}{suffix};')
    lines.append(");")
    lines.append(f"out tags center {limit};" if limit else "out tags center;")
    return "\n".join(lines)


def build_gazetteer_query(area: GeoArea, *, timeout_s: int = C.OVERPASS_QUERY_TIMEOUT_S) -> str:
    """The area's postcode boundaries and place nodes, tags only."""
    types = "|".join(C.GAZETTEER_PLACE_TYPES)
    return "\n".join([f"[out:json][timeout:{timeout_s}];", area_selector(area), "(",
                      '  rel["boundary"="postal_code"](area.region);',
                      f'  node["place"~"^({types})$"](area.region);', ");", "out tags;"])


def parse_gazetteer(data: dict[str, Any]) -> AreaGazetteer:
    """``postal_code`` tags of postcode boundaries (``;``/``,`` lists split) and the normalised
    ``name`` tags of place nodes."""
    postcodes: set[str] = set()
    places: set[str] = set()
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") == "relation" and tags.get("boundary") == "postal_code":
            postcodes.update(normalise_postcode(c) for c in re.split(r"[;,]", tags.get("postal_code") or "")
                             if c.strip())
        elif tags.get("place") in C.GAZETTEER_PLACE_TYPES and (name := tags.get("name")):
            if place := normalise_place(name):
                places.add(place)
    return AreaGazetteer(frozenset(postcodes), frozenset(places))


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


def candidate_tier(tags: dict[str, str]) -> int:
    """Website-first ordering: 0 = has a ``website``/``contact:website`` tag, 1 = has only an
    ``email``/``contact:email`` tag, 2 = neither (blank values do not count)."""
    if any((tags.get(k) or "").strip() for k in ("website", "contact:website")):
        return 0
    if any((tags.get(k) or "").strip() for k in ("email", "contact:email")):
        return 1
    return 2


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
    """Politeness for one endpoint: ≤ ``concurrency`` requests in flight, paced, daily cap."""

    def __init__(self, *, concurrency: int = C.OVERPASS_MAX_CONCURRENCY,
                 per_minute: float = C.OVERPASS_MAX_REQUESTS_PER_MINUTE,
                 daily_budget: int | None = C.OVERPASS_DAILY_BUDGET) -> None:
        self.concurrency = concurrency
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


def overpass_endpoints(overpass_url: str) -> tuple[str, ...]:
    """The public default URL gets the mirrors as failover endpoints; any other (self-hosted) URL
    stays alone, so its queries never leak to third parties."""
    if overpass_url == DEFAULT_OVERPASS_URL:
        return tuple(dict.fromkeys((overpass_url, *C.OVERPASS_MIRROR_URLS)))
    return (overpass_url,)


class OverpassGatePool:
    """Process-wide Overpass politeness: one:class:`OverpassGate` per endpoint URL, each with the
    unchanged limits."""

    def __init__(self, endpoints: Sequence[str], *, gates: dict[str, OverpassGate] | None = None,
                 **gate_kwargs: Any) -> None:
        self.endpoints: tuple[str, ...] = tuple(dict.fromkeys(endpoints))
        if not self.endpoints:
            raise ValueError("an Overpass gate pool needs at least one endpoint")
        self._gate_kwargs = gate_kwargs
        self.gates: dict[str, OverpassGate] = dict(gates or {})
        for url in self.endpoints:
            self.gate(url)

    @classmethod
    def for_settings(cls, settings: Settings, **gate_kwargs: Any) -> OverpassGatePool:
        """The pool for ``OVERPASS_URL`` (+ mirrors only for the public default)."""
        return cls(overpass_endpoints(settings.overpass_url), **gate_kwargs)

    @classmethod
    def of(cls, gate: OverpassGate | OverpassGatePool, overpass_url: str) -> OverpassGatePool:
        """A plain:class:`OverpassGate` is treated as a single-endpoint pool."""
        if isinstance(gate, OverpassGatePool):
            return gate
        return cls((overpass_url,), gates={overpass_url: gate})

    def gate(self, url: str) -> OverpassGate:
        if url not in self.gates:
            self.gates[url] = OverpassGate(**self._gate_kwargs)
        return self.gates[url]

    def endpoints_for(self, overpass_url: str) -> tuple[str, ...]:
        """This pool's endpoints (primary first) if it was built for ``overpass_url``, else that URL
        alone."""
        return self.endpoints if self.endpoints[0] == overpass_url else (overpass_url,)

    @property
    def concurrency(self) -> int:
        """Σ endpoint concurrency."""
        return sum(self.gate(url).concurrency for url in self.endpoints)


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def _is_runtime_error(data: dict[str, Any]) -> bool:
    return "runtime error" in str(data.get("remark") or "").lower()


def _n_elements(data: dict[str, Any] | None) -> int:
    return len((data or {}).get("elements") or [])


# --- adapter ------------------------------------------------------------------------------------
class _Retryable(Exception):
    pass


class _Unavailable(Exception):
    """A terminal, non-retryable failure of one endpoint; the message names the host."""


def _describe(exc: BaseException) -> str:
    """Short, data-free reason for warnings/logs, e.g."""
    if isinstance(exc, (_Retryable, _Unavailable)):
        return str(exc)                    # "http 504 at <host>" / "timeout at <host>" / "ConnectError at <host>"
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
    gate: OverpassGate | OverpassGatePool             # a plain gate = single-endpoint pool
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
    endpoints: tuple[str, ...] = ()                   # () = from the gate pool (primary first)
    pool: OverpassGatePool = field(init=False, repr=False)
    _gazetteers: dict[str, AreaGazetteer | None] = field(default_factory=dict, init=False, repr=False)
    _gazetteer_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    _gazetteer_warned: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.pool = OverpassGatePool.of(self.gate, self.overpass_url)
        if not self.endpoints:
            self.endpoints = self.pool.endpoints_for(self.overpass_url)

    @property
    def daily_budget(self) -> int | None:
        return self.pool.gate(self.endpoints[0]).daily_budget

    async def discover(self, slice_: SearchSlice) -> AsyncIterator[CompanyCandidate]:
        outcome = self.outcomes.setdefault(slice_, SliceOutcome())
        area = self.areas[slice_.area_id]
        industry = self.industries[slice_.industry_profile_id]
        query = build_query(area, industry, self.languages, limit=self.max_elements)
        try:
            data, endpoint = await self._post(query, outcome)
        except _BudgetExhausted as exc:
            self._exhaust(slice_, outcome, str(exc),
                          f"Overpass request budget exhausted for slice {slice_.region_label} × "
                          f"{slice_.industry_label}; results may be fewer than max_output.")
            return
        except (_Retryable, _Unavailable, httpx.HTTPError, ValueError) as exc:
            self._exhaust(slice_, outcome, "error",
                          f"Overpass unavailable for slice {slice_.region_label} × "
                          f"{slice_.industry_label} ({_describe(exc)}); slice skipped.")
            return
        if _is_runtime_error(data):
            self._exhaust(slice_, outcome, "runtime_error",
                          f"Overpass query for slice {slice_.region_label} × "
                          f"{slice_.industry_label} failed/timed out on the server "
                          f"({_host(endpoint)}); slice skipped.")
            # partial elements (if any) are still usable
        elements = data.get("elements") or []
        if len(elements) >= self.max_elements:        # truncated at the cap, not exhausted
            metrics.TILES_SATURATED.labels(source=self.name).inc()
            self._exhaust(slice_, outcome, "saturated",
                          f"Overpass results for slice {slice_.region_label} × "
                          f"{slice_.industry_label} truncated at {self.max_elements} elements; "
                          f"some candidates may be missed (no tiling in v0.3).")
        seen: set[str] = set()
        # candidates with a website first, then email-only, then the rest (stable sort)
        for el in sorted(elements, key=lambda e: candidate_tier(e.get("tags") or {})):
            cand = element_to_candidate(el)
            if cand is None or cand.source_ref in seen:
                continue
            if is_excluded(cand.name, industry.negative_keywords):
                continue
            seen.add(cand.source_ref)
            outcome.candidates += 1
            metrics.CANDIDATES_DISCOVERED.labels(country=slice_.country_code, source=self.name).inc()
            yield cand
        outcome.exhausted = True                  # one query per slice in v0.3
        outcome.reason = outcome.reason or "done"

    async def gazetteer(self, area: GeoArea) -> AreaGazetteer | None:
        """The area gazetteer, queried lazily and at most once per area and job through the gate
        pool (counts toward the job budget)."""
        if area.level == "country":
            return None
        async with self._gazetteer_locks.setdefault(area.id, asyncio.Lock()):
            if area.id not in self._gazetteers:
                self._gazetteers[area.id] = await self._fetch_gazetteer(area)
            return self._gazetteers[area.id]

    async def _fetch_gazetteer(self, area: GeoArea) -> AreaGazetteer | None:
        outcome = SliceOutcome()
        try:
            data, endpoint = await self._post(build_gazetteer_query(area), outcome)
        except _BudgetExhausted as exc:
            reason = "request budget exhausted" if str(exc) == "budget" else "daily budget exhausted"
        except (_Retryable, _Unavailable, httpx.HTTPError, ValueError) as exc:
            reason = _describe(exc)
        else:
            if not _is_runtime_error(data):
                return parse_gazetteer(data)
            reason = f"runtime error at {_host(endpoint)}"
        log.warning("overpass_gazetteer_unavailable", area=area.id, reason=reason)
        if not self._gazetteer_warned:
            self._gazetteer_warned = True
            self.warnings.append(f"Overpass area gazetteer unavailable ({reason}); the region check "
                                 "for non-OSM candidates uses OSM postcodes and area names only.")
        return None

    def _exhaust(self, slice_: SearchSlice, outcome: SliceOutcome, reason: str, warning: str) -> None:
        outcome.exhausted, outcome.reason = True, reason
        if warning not in self.warnings:
            self.warnings.append(warning)
        log.warning("overpass_slice_exhausted", reason=reason, area=slice_.area_id,
                    industry=slice_.industry_profile_id, detail=warning)

    def _failover_wait(self, state: RetryCallState) -> float:
        """No wait while an untried endpoint remains; afterwards the v0.3 exponential backoff,
        counted from the attempt that tried the last endpoint (identical for one endpoint)."""
        k = state.attempt_number - len(self.endpoints) + 1
        if k <= 0:
            return 0.0
        return min(self.retry_wait_s * 2 ** (k - 1), self.retry_wait_max_s)

    async def _post(self, query: str, outcome: SliceOutcome) -> tuple[dict[str, Any], str]:
        """One slice query: ``(data, endpoint)``. ≤ ``retry_attempts`` HTTP requests in total;
        attempt *n* goes to ``endpoints[(n - 1) % len(endpoints)]``."""
        client = self.client or httpx.AsyncClient(timeout=C.OVERPASS_HTTP_TIMEOUT_S)
        best: tuple[dict[str, Any], str] | None = None           # best runtime-error partial
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.retry_attempts), wait=self._failover_wait,
                retry=retry_if_exception_type(_Retryable), reraise=True,
            ):
                with attempt:
                    number = attempt.retry_state.attempt_number
                    url = self.endpoints[(number - 1) % len(self.endpoints)]
                    untried_left = number < len(self.endpoints) and number < self.retry_attempts
                    try:
                        data = await self._request(client, url, query, outcome)
                    except httpx.TransportError as exc:          # refused/DNS: fail over, no retry
                        reason = f"{type(exc).__name__} at {_host(url)}"
                        if untried_left:
                            raise _Retryable(reason) from exc
                        raise _Unavailable(reason) from exc
                    if not _is_runtime_error(data):
                        return data, url
                    if best is None or _n_elements(data) > _n_elements(best[0]):
                        best = (data, url)
                    if untried_left:
                        raise _Retryable(f"runtime error at {_host(url)}")
                    return best
        except (_Retryable, _Unavailable, _BudgetExhausted, httpx.HTTPError, ValueError):
            if best is not None:                          # any terminal failure after a partial:
                return best                               # partial elements are still usable
            raise
        finally:
            if self.client is None:
                await client.aclose()
        raise _Retryable("unreachable")  # pragma: no cover

    async def _request(self, client: httpx.AsyncClient, url: str, query: str,
                       outcome: SliceOutcome) -> dict[str, Any]:
        gate = self.pool.gate(url)
        if self.budget.exhausted:
            raise _BudgetExhausted("budget")
        if not gate.take_daily():
            raise _BudgetExhausted("daily_budget")
        self.budget.consume()
        outcome.requests += 1
        timeout: float | httpx.Timeout = C.OVERPASS_HTTP_TIMEOUT_S           # > [timeout:N]
        if url != self.endpoints[0]:                 # mirrors: fail fast when they do not answer
            timeout = httpx.Timeout(C.OVERPASS_HTTP_TIMEOUT_S, connect=C.OVERPASS_MIRROR_CONNECT_TIMEOUT_S)
        async with gate.semaphore:
            gate.in_flight += 1
            gate.max_in_flight = max(gate.max_in_flight, gate.in_flight)
            try:
                async with gate.limiter:
                    resp = await client.post(url, data={"data": query},
                                             headers={"User-Agent": self.user_agent}, timeout=timeout)
            except httpx.TimeoutException as exc:
                raise _Retryable(f"timeout at {_host(url)}") from exc
            finally:
                gate.in_flight -= 1
        if resp.status_code in RETRYABLE_STATUS:
            raise _Retryable(f"http {resp.status_code} at {_host(url)}")
        resp.raise_for_status()
        return resp.json()
