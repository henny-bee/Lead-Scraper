"""Polite HTTP fetcher (ARCHITECTURE.md §3.6, §9 crawler vars, §11 crawl metrics).

- ``User-Agent`` = ``CRAWLER_USER_AGENT``; global concurrency ``CRAWLER_GLOBAL_CONCURRENCY``.
- One connection per (registered) domain at a time, and ``CRAWLER_PER_DOMAIN_DELAY_S`` between the
  end of one request and the start of the next (raised to a robots.txt ``Crawl-delay`` up to
  ``ROBOTS_MAX_CRAWL_DELAY_S``).
- At most ``CRAWLER_MAX_PAGES_PER_DOMAIN`` page requests per domain and job (robots.txt excluded).
- Bodies are streamed: > ``CRAWLER_MAX_RESPONSE_MB`` (declared or actual) → aborted; non-HTML →
  skipped without reading the body.
- Redirects are followed manually; every hop is normalised and SSRF-checked (Q21) and, when a
  robots checker is attached, checked against robots.txt.
- ``crawl_requests_total{status_code}`` and ``crawl_duration_seconds`` are recorded per request.
All state is per job (one Fetcher per job, C7).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from leadscraper import constants as C
from leadscraper.crawler.website import (
    REDIRECT_STATUS,
    SKIP_REDIRECTS,
    HostGuard,
    Website,
    WebsiteRejected,
    normalize_website,
)
from leadscraper.observability import metrics
from leadscraper.settings import Settings

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
RobotsCheck = Callable[[Website], Awaitable[bool]]

SKIP_ROBOTS = "robots_disallowed"
SKIP_PAGE_LIMIT = "page_limit"
SKIP_TOO_LARGE = "too_large"
SKIP_NON_HTML = "non_html"
SKIP_HTTP_ERROR = "http_error"
SKIP_NETWORK = "network_error"


@dataclass(slots=True)
class FetchResult:
    url: str                              # requested (normalised) URL
    final_url: str | None = None          # after redirects
    final_site: Website | None = None
    status: int | None = None
    content_type: str | None = None
    html: str | None = None
    skipped: str | None = None            # one of the SKIP_* reasons; None = HTML page received

    @property
    def ok(self) -> bool:
        return self.skipped is None and self.html is not None


class Fetcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, guard: HostGuard, *,
                 clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep) -> None:
        self.user_agent = settings.crawler_user_agent
        self.delay_s = settings.crawler_per_domain_delay_s
        self.max_pages = settings.crawler_max_pages_per_domain
        self.max_bytes = settings.crawler_max_response_bytes
        self.client, self.guard = client, guard
        self.clock, self.sleep = clock, sleep
        self._global = asyncio.Semaphore(settings.crawler_global_concurrency)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_start: dict[str, float] = {}
        self.pages: dict[str, int] = defaultdict(int)
        self.domain_delay: dict[str, float] = {}
        self.robots: RobotsCheck | None = None
        self.unreachable_origins: set[str] = set()   # robots.txt fetch failed at network level

    def set_crawl_delay(self, domain: str, seconds: float | None) -> None:
        if seconds:
            self.domain_delay[domain] = min(max(seconds, self.delay_s), C.ROBOTS_MAX_CRAWL_DELAY_S)

    def pages_left(self, domain: str) -> int:
        return max(0, self.max_pages - self.pages[domain])

    async def fetch(self, url: str | Website, *, count_page: bool = True,
                    html_only: bool = True, max_bytes: int | None = None) -> FetchResult:
        try:
            site = url if isinstance(url, Website) else normalize_website(url)
        except WebsiteRejected as exc:
            return FetchResult(url=str(url), skipped=exc.reason)
        result = FetchResult(url=site.url)
        for _ in range(C.MAX_REDIRECTS + 1):
            try:
                await self.guard.check(site)
            except WebsiteRejected as exc:
                result.skipped = exc.reason
                return result
            if count_page:
                if self.robots is not None and not await self.robots(site):
                    result.skipped = SKIP_ROBOTS
                    return result
            location = await self._request(site, result, count_page=count_page,
                                           html_only=html_only, max_bytes=max_bytes)
            if location is None:
                return result
            try:
                site = normalize_website(urljoin(site.url, location))
            except WebsiteRejected as exc:
                result.skipped = exc.reason
                return result
        result.skipped = SKIP_REDIRECTS
        return result

    async def _request(self, site: Website, result: FetchResult, *, count_page: bool,
                       html_only: bool, max_bytes: int | None) -> str | None:
        """One polite GET. Returns the redirect target, or None when ``result`` is final."""
        domain = site.registered_domain
        limit = max_bytes or self.max_bytes
        async with self._locks[domain]:
            if count_page and self.pages[domain] >= self.max_pages:   # checked under the lock
                result.skipped = SKIP_PAGE_LIMIT
                return None
            wait = self._next_start.get(domain, 0.0) - self.clock()
            if wait > 0:
                await self.sleep(wait)
            if count_page:
                self.pages[domain] += 1
            started = time.perf_counter()
            label = "error"
            try:
                async with self._global:
                    async with self.client.stream(
                            "GET", site.url, follow_redirects=False,
                            headers={"User-Agent": self.user_agent,
                                     "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"},
                    ) as resp:
                        label = str(resp.status_code)
                        result.status = resp.status_code
                        if resp.status_code in REDIRECT_STATUS and resp.headers.get("location"):
                            return resp.headers["location"]
                        result.final_url, result.final_site = site.url, site
                        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                        result.content_type = ctype or None
                        if resp.status_code >= 400:
                            result.skipped = SKIP_HTTP_ERROR
                            return None
                        if html_only and ctype not in C.HTML_CONTENT_TYPES:
                            result.skipped = SKIP_NON_HTML
                            return None
                        declared = resp.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > limit:
                            result.skipped = SKIP_TOO_LARGE
                            return None
                        body = bytearray()
                        async for chunk in resp.aiter_bytes():
                            body += chunk
                            if len(body) > limit:
                                result.skipped = SKIP_TOO_LARGE
                                return None
                        result.html = bytes(body).decode(resp.charset_encoding or "utf-8",
                                                         errors="replace")
                        return None
            except httpx.HTTPError:
                result.skipped = SKIP_NETWORK
                return None
            finally:
                metrics.CRAWL_REQUESTS.labels(status_code=label).inc()
                metrics.CRAWL_DURATION.observe(time.perf_counter() - started)
                delay = self.domain_delay.get(domain, self.delay_s)
                self._next_start[domain] = self.clock() + delay
