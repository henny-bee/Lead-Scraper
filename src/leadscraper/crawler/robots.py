"""Robots.txt handling with protego."""

from __future__ import annotations

import httpx
from protego import Protego

from leadscraper import constants as C
from leadscraper.crawler.fetcher import SKIP_NETWORK, Fetcher
from leadscraper.crawler.website import Website

_ALLOW_ALL = Protego.parse("")
#: robots.txt keeps the full read timeout, so the tighter page read timeout never turns a slow
#: robots.txt into a disallow-all (RFC 9309 semantics below are unchanged).
ROBOTS_TIMEOUT = httpx.Timeout(C.CRAWLER_HTTP_TIMEOUT_S, connect=C.CRAWLER_CONNECT_TIMEOUT_S)
_DISALLOW_ALL = Protego.parse("User-agent: *\nDisallow: /\n")


class RobotsCache:
    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.user_agent = fetcher.user_agent
        self._rules: dict[str, Protego] = {}

    def attach(self) -> RobotsCache:
        """Make the fetcher check every page (and redirect hop) against robots.txt."""
        self.fetcher.robots = self.allowed
        self.fetcher.sitemaps = self.sitemaps
        return self

    async def sitemaps(self, site: Website) -> list[str]:
        """``Sitemap:`` URLs of the site's robots.txt; empty for the allow-all and disallow-all
        defaults."""
        return list((await self.rules_for(site)).sitemaps)

    async def rules_for(self, site: Website) -> Protego:
        key = site.origin
        if key not in self._rules:
            self._rules[key] = await self._load(site)
        return self._rules[key]

    async def _load(self, site: Website) -> Protego:
        res = await self.fetcher.fetch(f"{site.origin}/robots.txt", count_page=False,
                                       html_only=False, max_bytes=C.ROBOTS_MAX_BYTES,
                                       timeout=ROBOTS_TIMEOUT)
        if res.html is not None and res.status is not None and 200 <= res.status < 300:
            rules = Protego.parse(res.html)
            self.fetcher.set_crawl_delay(site.registered_domain, rules.crawl_delay(self.user_agent))
            return rules
        if res.status is not None and 400 <= res.status < 500:
            return _ALLOW_ALL                    # "unavailable" → no restrictions (RFC 9309)
        if res.status is None and res.skipped == SKIP_NETWORK:
            self.fetcher.unreachable_origins.add(site.origin)
        return _DISALLOW_ALL                     # 5xx, network error, oversize, blocked redirect

    async def allowed(self, site: Website) -> bool:
        rules = await self.rules_for(site)
        return rules.can_fetch(site.url, self.user_agent)
