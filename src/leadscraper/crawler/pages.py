"""Homepage → legal-notice / contact page discovery."""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import ipaddress
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from selectolax.parser import HTMLParser

from leadscraper.crawler.fetcher import SKIP_HTTP_ERROR, SKIP_NETWORK, FetchResult, Fetcher
from leadscraper.crawler.js_shell import is_js_shell
from leadscraper.crawler.sitemap import is_gzip, is_index, parse_locs, pick_children
from leadscraper.crawler.website import (
    SKIP_PRIVATE,
    Website,
    WebsiteRejected,
    normalize_website,
    registered_domain,
)
from leadscraper.observability import metrics
from leadscraper.observability.logging import get_logger
from leadscraper.services.resolver.geo import norm

log = get_logger(__name__)

_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "#", "sms:", "fax:")
SKIP_SITE_BUDGET = "site_budget"             # the homepage did not arrive within the site budget


@dataclass(slots=True)
class Page:
    url: str
    html: str
    kind: str                         # "home" | "legal" | "contact" | "fallback" | "other"
    is_legal: bool = False


@dataclass(slots=True)
class SiteCrawl:
    website: Website                  # confirmed final website (origin = output `website`)
    lang: str | None = None
    pages: list[Page] = field(default_factory=list)
    skipped: str | None = None        # reason when the homepage could not be fetched


def html_lang(html: str) -> str | None:
    node = HTMLParser(html).css_first("html")
    lang = (node.attributes.get("lang") or "").strip() if node else ""
    return lang.split("-")[0].split("_")[0].lower() or None if lang else None


def _match(text: str, keywords: Sequence[str]) -> int | None:
    """Index of the first keyword contained in ``text`` (both normalised), else None."""
    t = norm(text)
    for i, kw in enumerate(keywords):
        k = norm(kw)
        if k and k in t:
            return i
    return None


_SEGMENT_EXT = re.compile(r"\.(?:s?html?|php\d?|aspx?|jsp|cfm|cgi)$", re.IGNORECASE)


def _exact(label: str, path: str, keywords: Sequence[str]) -> int | None:
    """Index of the first keyword equal to a whole path segment (extension stripped, e.g."""
    parts = {norm(label)} | {norm(_SEGMENT_EXT.sub("", seg)) for seg in path.split("/") if seg}
    parts.discard("")
    for i, kw in enumerate(keywords):
        k = norm(kw)
        if k and k in parts:
            return i
    return None


def find_contact_links(html: str, base_url: str, keywords: Sequence[str]) -> list[tuple[str, str]]:
    """``[(absolute_url, matched_keyword)]`` on the same registered domain: exact path-segment /
    link-label matches before substring matches, then keyword priority (config order: legal
    notice first), then document order; duplicates removed."""
    base = normalize_website(base_url)
    hits: list[tuple[int, int, int, str, str]] = []
    for pos, a in enumerate(HTMLParser(html).css("a[href]")):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(_SKIP_SCHEMES):
            continue
        url = urljoin(base.url, href)
        try:
            target = normalize_website(url)
        except WebsiteRejected:
            continue
        if target.registered_domain != base.registered_domain:
            continue
        raw_path = unquote(urlsplit(target.url).path)
        label = a.text(separator=" ")
        exact = _exact(label, raw_path, keywords)
        if exact is not None:
            hits.append((0, exact, pos, target.url, keywords[exact]))
            continue
        idx = _match(f"{label} {raw_path.replace('/', ' ')}", keywords)
        if idx is not None:
            hits.append((1, idx, pos, target.url, keywords[idx]))
    out: list[tuple[str, str]] = []
    seen = {base.url.rstrip("/")}
    for *_, url, kw in sorted(hits, key=lambda h: h[:3]):
        key = url.split("#")[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append((url, kw))
    return out


def _is_legal(text: str, legal_markers: Sequence[str]) -> bool:
    return _match(text, legal_markers) is not None


def _classify(links: Sequence[tuple[str, str]], legal_markers: Sequence[str],
              contact_markers: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """Split matched links into legal / contact / other (neither legal nor contact)."""
    legal, contact, other = [], [], []
    for url, kw in links:
        if _is_legal(kw, legal_markers):
            legal.append(url)
        elif _match(kw, contact_markers) is not None:
            contact.append(url)
        else:
            other.append(url)
    return legal, contact, other


_STOP, _SKIP, _OK, _MISS = "stop", "skip", "ok", "miss"


class _PageStep:
    """One non-home page fetch with the v0.3 loop rules: page budget, URL dedup, site deadline,
    fail-fast, off-site filter and the ``done`` early exit."""

    def __init__(self, fetcher: Fetcher, crawl: SiteCrawl, *, deadline: float | None,
                 save_dir: Path | None, max_consecutive_failures: int | None,
                 done: Callable[[list[Page]], bool] | None) -> None:
        self.fetcher, self.crawl = fetcher, crawl
        self.domain = crawl.website.registered_domain
        self.deadline, self.save_dir = deadline, save_dir
        self.max_failures, self.done = max_consecutive_failures, done
        self.fetched = {crawl.website.url.rstrip("/")}
        self.failures = 0

    def pages_left(self) -> int:
        return self.fetcher.pages_left(self.domain)

    async def request(self, url: str, *, html_only: bool = True) -> tuple[str, FetchResult | None]:
        """The shared rules around one page request (it counts toward the page budget)."""
        if self.pages_left() <= 0:
            return _STOP, None
        if url.rstrip("/") in self.fetched:
            return _SKIP, None
        if _expired(self.deadline):
            return _STOP, None
        try:
            res = await _within(self.deadline, self.fetcher.fetch(url, html_only=html_only))
        except TimeoutError:
            return _STOP, None                          # site budget spent: keep what we have
        self.fetched.add(url.rstrip("/"))
        self.failures = self.failures + 1 if res.skipped == SKIP_NETWORK else 0
        if self.max_failures is not None and self.failures >= self.max_failures:
            return _STOP, None                          # fail-fast: the site went dark
        return _MISS, res

    async def fetch(self, url: str, kind: str, is_legal: bool) -> str:
        status, res = await self.request(url)
        if res is None:
            return status
        if not res.ok or res.final_site is None:
            return _MISS
        if res.final_site.registered_domain != self.domain:
            return _MISS                                # redirected off-site: not the company's page
        self.crawl.pages.append(Page(res.final_site.url, res.html or "", kind, is_legal))
        _save(self.save_dir, res.final_site.url, res.html or "")
        if self.done is not None and self.done(self.crawl.pages):
            return _STOP                                # early exit
        return _OK


def _is_sitemap_body(res: FetchResult) -> bool:
    ctype = (res.content_type or "").lower()
    return res.html is not None and res.skipped is None and ("xml" in ctype or ctype.startswith("text/"))


async def _sitemap_links(step: _PageStep, keywords: Sequence[str]) -> tuple[str, list[tuple[str, str]]]:
    """Read the site's sitemap (robots ``Sitemap:`` on the same domain, else ``/sitemap.xml``; one
    index level, ``SITEMAP_MAX_CHILDREN``) — every fetch counts toward the page budget — and rank
    its same-domain ``<loc>`` URLs like homepage links."""
    site = step.crawl.website
    listed = await step.fetcher.sitemaps(site) if step.fetcher.sitemaps is not None else []
    own = [u for u in listed if not is_gzip(u) and registered_domain(u) == site.registered_domain]
    status, res = await step.request(own[0] if own else f"{site.origin}/sitemap.xml", html_only=False)
    if res is None or not _is_sitemap_body(res):
        return status, []
    locs = parse_locs(res.html or "")
    if is_index(res.html or ""):
        children, locs = pick_children(locs), []
        for child in children:
            status, res = await step.request(child, html_only=False)
            if status == _STOP:
                return _STOP, []
            if res is not None and _is_sitemap_body(res):
                locs += parse_locs(res.html or "")
    anchors = "".join(f'<a href="{html_lib.escape(u, quote=True)}"></a>' for u in locs)
    return _MISS, find_contact_links(anchors, site.url, keywords)


async def _crawl_by_kind(step: _PageStep, links: Sequence[tuple[str, str]],
                         fallback_by_kind: tuple[Sequence[str], Sequence[str]],
                         legal_markers: Sequence[str], contact_markers: Sequence[str],
                         keywords: Sequence[str] = (), use_sitemap: bool = False) -> None:
    """Page-budget order: legal phase → contact phase → other matches."""
    legal, contact, other = _classify(links, legal_markers, contact_markers)
    origin = step.crawl.website.origin

    def probe(path: str) -> str:
        return urljoin(origin + "/", path.lstrip("/"))

    found = {"legal": False, "contact": False, "other": False}

    async def go(url: str, kind: str, is_legal: bool, group: str) -> bool:
        status = await step.fetch(url, kind, is_legal)
        if status == _OK:
            found[group] = True
        return status != _STOP

    legal_fallbacks, contact_fallbacks = fallback_by_kind
    if legal:
        for url in legal:
            if not await go(url, "legal", True, "legal"):
                return
    elif legal_fallbacks:
        first, *rest = legal_fallbacks
        if not await go(probe(first), "fallback", True, "legal"):
            return
        # --- the sitemap step (after the first legal fallback, before the remaining ones) ---------
        if use_sitemap and not found["legal"] and step.pages_left() > 0:
            status, ranked = await _sitemap_links(step, keywords)
            if status == _STOP:
                return
            s_legal, s_contact, _ = _classify(ranked, legal_markers, contact_markers)
            if s_legal and not await go(s_legal[0], "legal", True, "legal"):
                return
            if s_contact and not await go(s_contact[0], "contact", False, "contact"):
                return
        for path in rest:
            if found["legal"]:
                break                                   # a legal page is known: no blind probes
            if not found["contact"] and step.pages_left() <= 1:
                break                                   # one page stays for the contact phase
            if not await go(probe(path), "fallback", True, "legal"):
                return
    if contact:
        for url in contact:
            if not await go(url, "contact", False, "contact"):
                return
    else:
        for path in contact_fallbacks:
            if found["contact"]:
                break
            if not await go(probe(path), "fallback", False, "contact"):
                return
    for url in other:
        if not await go(url, "other", False, "other"):
            return


def _save(save_dir: Path | None, url: str, html: str) -> None:
    if save_dir is None:
        return
    save_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"
    (save_dir / name).write_text(html, encoding="utf-8")


def flip_www(website: Website) -> Website | None:
    """``firma.de`` ↔ ``www.firma.de`` (same scheme, port and path); None for IP literals."""
    host = website.host
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None                                    # IP literal: nothing to flip
    except ValueError:
        pass
    other = host[4:] if host.startswith("www.") else f"www.{host}"
    if not other or "." not in other:
        return None
    try:
        return normalize_website(website.url.replace(f"//{host}", f"//{other}", 1))
    except WebsiteRejected:
        return None


def _seed_failed(home: FetchResult, website: Website, fetcher: Fetcher) -> bool:
    """Unresolvable host, network error (also on its robots.txt, which the Fetcher records in
    ``unreachable_origins``), or HTTP 5xx other than 503."""
    if home.skipped == SKIP_PRIVATE and website.host in fetcher.guard.unresolvable:
        return True
    if home.skipped == SKIP_NETWORK or website.origin in fetcher.unreachable_origins:
        return True
    return (home.skipped == SKIP_HTTP_ERROR and home.status is not None
            and home.status >= 500 and home.status != 503)


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


async def _within(deadline: float | None, fetch: Awaitable[FetchResult]) -> FetchResult:
    """Await ``fetch``, cancelled at ``deadline`` (``TimeoutError``); no limit when ``None``."""
    if deadline is None:
        return await fetch
    async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
        return await fetch


async def crawl_site(fetcher: Fetcher, website: Website, *, keywords: Sequence[str],
                     fallback_paths: Sequence[str], legal_markers: Sequence[str] = (),
                     save_dir: Path | None = None, deadline: float | None = None,
                     max_consecutive_failures: int | None = None,
                     done: Callable[[list[Page]], bool] | None = None,
                     fallback_by_kind: tuple[Sequence[str], Sequence[str]] | None = None,
                     contact_markers: Sequence[str] = (), seed_variants: bool = False,
                     use_sitemap: bool = False) -> SiteCrawl:
    """Crawl one company website: homepage + matching legal/contact pages (≤ page limit)."""
    try:
        home: FetchResult = await _within(deadline, fetcher.fetch(website))
        unreachable = home.skipped == SKIP_NETWORK or website.origin in fetcher.unreachable_origins
        if seed_variants and not home.ok and _seed_failed(home, website, fetcher):
            flipped = flip_www(website)
            if flipped is not None:
                alt = await _within(deadline, fetcher.fetch(flipped))
                if alt.ok:
                    home = alt
        if not home.ok and website.url.startswith("https://") and unreachable:  # no TLS → try http
            home = await _within(deadline, fetcher.fetch(
                normalize_website("http://" + website.url[len("https://"):])))
    except TimeoutError:
        return SiteCrawl(website=website, skipped=SKIP_SITE_BUDGET)
    if not home.ok or home.final_site is None:
        return SiteCrawl(website=website, skipped=home.skipped or "no_html")
    final = home.final_site
    crawl = SiteCrawl(website=final, lang=html_lang(home.html or ""))
    crawl.pages.append(Page(final.url, home.html or "", "home"))
    _save(save_dir, final.url, home.html or "")
    if is_js_shell(home.html or ""):                    # measured only, nothing is rendered
        metrics.CRAWL_JS_SHELLS.inc()
        log.info("js_shell_detected", domain=final.registered_domain)

    links = find_contact_links(home.html or "", final.url, keywords)
    step = _PageStep(fetcher, crawl, deadline=deadline, save_dir=save_dir,
                     max_consecutive_failures=max_consecutive_failures, done=done)
    if fallback_by_kind is not None:
        await _crawl_by_kind(step, links, fallback_by_kind, legal_markers, contact_markers,
                             keywords=keywords, use_sitemap=use_sitemap)
        return crawl
    candidates = [
        (url, "legal" if _is_legal(kw, legal_markers) else "contact", _is_legal(kw, legal_markers))
        for url, kw in links]
    if not candidates:
        candidates = [(urljoin(final.origin + "/", p.lstrip("/")), "fallback",
                       _is_legal(p, legal_markers)) for p in fallback_paths]
    for url, kind, is_legal in candidates:
        if await step.fetch(url, kind, is_legal) == _STOP:
            break
    return crawl
