"""robots.txt handling with protego (ARCHITECTURE.md §3.6 "robots.txt is respected").

One :class:`RobotsCache` per job (C7). Rules follow RFC 9309: ``2xx`` → parse; ``4xx`` (incl.
404) → no restrictions; ``5xx`` / network error / redirect to a non-public host → treated as a
complete disallow (conservative). ``Crawl-delay`` for our user agent raises the per-domain delay
(capped). robots.txt requests do not count against the per-domain page limit.
"""

from __future__ import annotations

from protego import Protego

from leadscraper import constants as C
from leadscraper.crawler.fetcher import SKIP_NETWORK, Fetcher
from leadscraper.crawler.website import Website

_ALLOW_ALL = Protego.parse("")
_DISALLOW_ALL = Protego.parse("User-agent: *\nDisallow: /\n")


class RobotsCache:
    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.user_agent = fetcher.user_agent
        self._rules: dict[str, Protego] = {}

    def attach(self) -> RobotsCache:
        """Make the fetcher check every page (and redirect hop) against robots.txt."""
        self.fetcher.robots = self.allowed
        return self

    async def rules_for(self, site: Website) -> Protego:
        key = site.origin
        if key not in self._rules:
            self._rules[key] = await self._load(site)
        return self._rules[key]

    async def _load(self, site: Website) -> Protego:
        res = await self.fetcher.fetch(f"{site.origin}/robots.txt", count_page=False,
                                       html_only=False, max_bytes=C.ROBOTS_MAX_BYTES)
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
