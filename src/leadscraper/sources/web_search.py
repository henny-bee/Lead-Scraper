"""Web search backends + the process-wide:class:`SearchGate`."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx
from protego import Protego
from selectolax.parser import HTMLParser, Node

from leadscraper import constants as C
from leadscraper.crawler.website import WebsiteRejected, normalize_website
from leadscraper.domain.models import CompanyCandidate, GeoArea, IndustryProfile, SearchSlice
from leadscraper.observability import metrics
from leadscraper.observability.logging import get_logger
from leadscraper.services.resolver.industry import keywords_for
from leadscraper.settings import Settings

log = get_logger(__name__)

SOURCE_NAME = "web_search"
BLOCK_STATUS = frozenset({202, 403, 429})
ANOMALY_MARKERS = ("anomaly-modal", "unusual traffic")
DEFAULT_USER_AGENT: str = Settings.model_fields["crawler_user_agent"].default
ClientFactory = Callable[[], httpx.AsyncClient]


@dataclass(slots=True, frozen=True)
class SearchResult:
    url: str                          # origin of the result (normalised)
    title: str
    snippet: str = ""


class SearchBlocked(Exception):
    """The backend refused or challenged us (202/403/429, anomaly/CAPTCHA page)."""


class SearchUnavailable(Exception):
    """Network error, timeout or another non-success answer."""


class SearchBackend(Protocol):
    name: str
    url: str
    user_agent: str

    async def search(self, query: str, *, language: str, country: str) -> list[SearchResult]: ...

    async def fetch_robots(self) -> tuple[int | None, str]: ...


def filter_results(raw: Iterable[tuple[str, str, str]],
                   limit: int = C.WEB_SEARCH_MAX_RESULTS) -> list[SearchResult]:
    """SSRF/directory filter: keep http(s) company origins only, deduplicated, at most ``limit``."""
    out: list[SearchResult] = []
    seen: set[str] = set()
    for url, title, snippet in raw:
        try:
            site = normalize_website(url)
        except WebsiteRejected:
            continue
        if site.origin in seen:
            continue
        seen.add(site.origin)
        out.append(SearchResult(site.origin, " ".join(title.split()), " ".join(snippet.split())))
        if len(out) >= limit:
            break
    return out


def decode_ddg_href(href: str) -> str | None:
    """``//duckduckgo.com/l/?uddg=<url-encoded>&rut=…`` → the target URL; plain http(s) as is."""
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.endswith("duckduckgo.com"):
        target = parse_qs(parts.query).get("uddg")
        return target[0] if target and parts.path.startswith("/l/") else None
    return href if parts.scheme in ("http", "https") else None


def _classes(node: Node) -> set[str]:
    return set((node.attributes.get("class") or "").split())


def _result_container(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None:
        if any(c == "result" or c.startswith("result--") for c in _classes(cur)):
            return cur
        cur = cur.parent
    return None


def parse_ddg_html(html: str) -> list[SearchResult]:
    """Organic results of a DuckDuckGo HTML page (ads skipped); malformed markup → ``[]``."""
    raw: list[tuple[str, str, str]] = []
    for a in HTMLParser(html or "").css("a.result__a"):
        container = _result_container(a)
        if container is not None and "result--ad" in _classes(container):
            continue
        url = decode_ddg_href(a.attributes.get("href") or "")
        if not url:
            continue
        snippet = container.css_first(".result__snippet") if container is not None else None
        raw.append((url, a.text(separator=" "), snippet.text(separator=" ") if snippet else ""))
    return filter_results(raw)


def parse_searxng_json(text: str) -> list[SearchResult]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    raw = [(str(r.get("url") or ""), str(r.get("title") or ""), str(r.get("content") or ""))
           for r in results if isinstance(r, dict)]
    return filter_results(raw)


class _HttpBackend:
    name = "http"

    def __init__(self, url: str, *, user_agent: str = DEFAULT_USER_AGENT,
                 client_factory: ClientFactory | None = None) -> None:
        self.url = url
        self.user_agent = user_agent
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(
            timeout=C.WEB_SEARCH_TIMEOUT_S, follow_redirects=False))

    @property
    def robots_url(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}/robots.txt"

    async def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            async with self._client_factory() as client:
                return await client.get(url, params=params, timeout=C.WEB_SEARCH_TIMEOUT_S,
                                        headers={"User-Agent": self.user_agent})
        except httpx.HTTPError as exc:
            raise SearchUnavailable(type(exc).__name__) from exc

    async def fetch_robots(self) -> tuple[int | None, str]:
        try:
            resp = await self._get(self.robots_url)
        except SearchUnavailable:
            return None, ""
        return resp.status_code, resp.text

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code in BLOCK_STATUS:
            raise SearchBlocked(f"http {resp.status_code}")
        if resp.status_code >= 300:
            raise SearchUnavailable(f"http {resp.status_code}")
        body = resp.text.lower()
        if any(marker in body for marker in ANOMALY_MARKERS):
            raise SearchBlocked("anomaly page")


class DuckDuckGoHtml(_HttpBackend):
    """DuckDuckGo's HTML endpoint (robots.txt of ``html.duckduckgo.com``: ``Allow: /``)."""

    name = "duckduckgo"

    def __init__(self, url: str = C.DUCKDUCKGO_HTML_URL, **kwargs: Any) -> None:
        super().__init__(url, **kwargs)

    async def search(self, query: str, *, language: str, country: str) -> list[SearchResult]:
        resp = await self._get(self.url, {"q": query, "kl": f"{country.lower()}-{language.lower()}"})
        self._check(resp)
        return parse_ddg_html(resp.text)


class SearxngJson(_HttpBackend):
    """A SearXNG-compatible JSON API: ``GET {url}/search?q=…&format=json&language=…``."""

    name = "searxng"

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__(url.rstrip("/"), **kwargs)

    @property
    def search_url(self) -> str:
        return f"{self.url}/search"

    async def search(self, query: str, *, language: str, country: str) -> list[SearchResult]:
        resp = await self._get(self.search_url, {"q": query, "format": "json", "language": language})
        self._check(resp)
        return parse_searxng_json(resp.text)


def build_search_backend(settings: Settings, *,
                         client_factory: ClientFactory | None = None) -> SearchBackend | None:
    """The backend configured by ``WEB_SEARCH_URL``; ``None`` when search is off."""
    kind = settings.web_search_backend
    if kind is None:
        return None
    if kind == "duckduckgo":
        return DuckDuckGoHtml(settings.web_search_url, user_agent=settings.crawler_user_agent,
                              client_factory=client_factory)
    return SearxngJson(settings.web_search_url, user_agent=settings.crawler_user_agent,
                       client_factory=client_factory)


# --- gate --------------------------------------------------------------------------------------------
@dataclass
class SearchJob:
    """Per-job search state."""

    budget: int = C.WEB_SEARCH_BUDGET_PER_JOB
    used: int = 0
    disabled: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.budget

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)


class SearchGate:
    """Process-wide politeness and safety for the search backend (see the module docstring)."""

    def __init__(self, *, min_interval_s: float = C.WEB_SEARCH_MIN_INTERVAL_S,
                 daily_budget: int | None = C.WEB_SEARCH_DAILY_BUDGET,
                 cooldown_s: float = C.WEB_SEARCH_COOLDOWN_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.min_interval_s, self.daily_budget, self.cooldown_s = min_interval_s, daily_budget, cooldown_s
        self.clock, self.sleep = clock, sleep
        self._lock = asyncio.Lock()                      # one request in flight
        self._next_start = 0.0
        self._day, self._used_today = dt.date.today(), 0
        self._robots: dict[str, tuple[str, float]] = {}  # robots URL → (state, valid until)
        self.paused_until = 0.0                          # circuit breaker (process-wide)
        self.in_flight = self.max_in_flight = 0
        self.requests = 0

    def _take_daily(self) -> bool:
        today = dt.date.today()
        if today != self._day:
            self._day, self._used_today = today, 0
        if self.daily_budget is not None and self._used_today >= self.daily_budget:
            return False
        self._used_today += 1
        return True

    async def _spaced(self, call: Callable[[], Awaitable[Any]]) -> Any:
        wait = self._next_start - self.clock()
        if wait > 0:
            await self.sleep(wait)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.requests += 1
        try:
            return await call()
        finally:
            self.in_flight -= 1
            self._next_start = self.clock() + self.min_interval_s

    async def _robots_allowed(self, backend: SearchBackend, job: SearchJob) -> bool:
        key = getattr(backend, "robots_url", backend.url)
        state, until = self._robots.get(key, ("unknown", 0.0))
        if state == "unreachable" and self.clock() >= until:
            state = "unknown"                            # re-check after the cooldown
        if state == "unknown":
            state = await self._load_robots(backend, key)
        if state == "disallowed":
            job.warn("Web search is disabled: the search backend's robots.txt disallows it.")
            return False
        if state == "unreachable":
            job.warn("Web search is paused: the search backend's robots.txt could not be read.")
            return False
        return True

    async def _load_robots(self, backend: SearchBackend, key: str) -> str:
        status, text = await self._spaced(backend.fetch_robots)
        if status is not None and 200 <= status < 300:
            path = getattr(backend, "search_url", backend.url)
            state = "ok" if Protego.parse(text).can_fetch(path, backend.user_agent) else "disallowed"
            if state == "disallowed":
                log.warning("web_search_robots_disallowed", backend=backend.name)
            self._robots[key] = (state, math.inf)        # explicit rules: for the process lifetime
        elif status is not None and 400 <= status < 500:
            state = "ok"                                 # RFC 9309: unavailable → no restrictions
            self._robots[key] = (state, math.inf)
        else:
            state = "unreachable"                        # 5xx / network / timeout
            self._robots[key] = (state, self.clock() + self.cooldown_s)
            log.warning("web_search_robots_unreachable", backend=backend.name, status=status)
        return state

    def _may_search(self, job: SearchJob) -> bool:
        if job.disabled:
            return False
        if self.clock() < self.paused_until:
            job.disabled = True
            job.warn("Web search is paused after the search backend blocked requests; "
                     "results may be fewer than max_output.")
            return False
        if job.exhausted:
            job.warn("Web search budget exhausted for this job; results may be fewer than max_output.")
            return False
        return True

    async def search(self, backend: SearchBackend, query: str, *, language: str, country: str,
                     job: SearchJob) -> list[SearchResult]:
        """Results for ``query``, or ``[]`` when the gate says no (budget, cap, robots, breaker)."""
        if not self._may_search(job):
            return []
        async with self._lock:
            if not self._may_search(job):              # re-checked: callers queue on the lock
                return []
            if not await self._robots_allowed(backend, job):
                return []
            if not self._take_daily():
                job.warn("Daily web search limit reached; results may be fewer than max_output.")
                return []
            job.used += 1
            metrics.SOURCE_BUDGET_USED.labels(source=SOURCE_NAME).inc()
            try:
                return await self._spaced(lambda: backend.search(query, language=language,
                                                                 country=country))
            except SearchBlocked as exc:
                job.disabled = True
                self.paused_until = self.clock() + self.cooldown_s
                job.warn("Web search was blocked by the search backend and is paused; "
                         "results may be fewer than max_output.")
                log.warning("web_search_blocked", backend=backend.name, reason=str(exc))
                return []
            except SearchUnavailable as exc:
                log.warning("web_search_unavailable", backend=backend.name, reason=str(exc))
                return []


# --- web-search discovery adapter -------------------------------------------------------------------
def discovery_budget(job_budget: int = C.WEB_SEARCH_BUDGET_PER_JOB) -> int:
    """The discovery share of the job's search budget."""
    return math.floor(job_budget * C.WEB_DISCOVERY_BUDGET_FRACTION + 1e-9)


@dataclass
class WebSearchAdapter:
    """``SourceAdapter`` for companies found by web search."""

    backend: SearchBackend
    gate: SearchGate
    job: SearchJob
    areas: dict[str, GeoArea]
    industries: dict[str, IndustryProfile]
    languages: Sequence[str]
    country: str
    queries_per_slice: int = C.WEB_DISCOVERY_QUERIES_PER_SLICE
    budget: int = field(default_factory=discovery_budget)
    name: str = SOURCE_NAME
    countries: frozenset[str] | None = None
    used: int = 0                                     # discovery queries issued (≤ budget)
    issued: dict[tuple[str, str], int] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)       # result origins already yielded

    @property
    def daily_budget(self) -> int | None:
        return self.gate.daily_budget

    def queries(self, slice_: SearchSlice) -> list[str]:
        """``"<keyword> <area name>"`` for the slice's first local keywords (profile language
        order)."""
        area = self.areas[slice_.area_id]
        industry = self.industries[slice_.industry_profile_id]
        words = dict.fromkeys(w.strip() for ws in keywords_for(industry, self.languages).values()
                              for w in ws if w.strip())
        return [f"{w} {area.name}" for w in list(words)[:self.queries_per_slice]]

    def has_more(self, slice_: SearchSlice) -> bool:
        key = (slice_.area_id, slice_.industry_profile_id)
        return (not self.job.disabled and self.used < self.budget
                and self.issued.get(key, 0) < len(self.queries(slice_)))

    async def discover(self, slice_: SearchSlice) -> AsyncIterator[CompanyCandidate]:
        """The slice's next discovery query (the first call is the first pass) → one candidate per
        new result origin."""
        if not self.has_more(slice_):
            return
        key = (slice_.area_id, slice_.industry_profile_id)
        query = self.queries(slice_)[self.issued.get(key, 0)]
        self.issued[key] = self.issued.get(key, 0) + 1
        self.used += 1                                # counted when issued: never above the share
        language = self.languages[0] if self.languages else "en"
        results = await self.gate.search(self.backend, query, language=language,
                                         country=self.country, job=self.job)
        for result in results:
            if result.url in self.seen:
                continue
            self.seen.add(result.url)
            metrics.CANDIDATES_DISCOVERED.labels(country=slice_.country_code, source=self.name).inc()
            yield CompanyCandidate(name=result.title, source=self.name, source_ref=result.url,
                                   website=result.url, coords_storable=False)
