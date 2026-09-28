"""Homepage → legal-notice / contact page discovery (ARCHITECTURE.md §3.6).

The crawler opens the homepage, reads ``<html lang>``, collects links whose text or URL matches
``CountryProfile.contact_keywords`` (official languages + English; legal-notice words first) and,
when nothing matches, tries the common fallback paths (``/impressum``, ``/mentions-legales``,
``/aviso-legal``, ``/contact``). Only links on the company's own registered domain
are followed; the per-domain page limit, robots.txt, size/type limits and delays are enforced by
:class:`~leadscraper.crawler.fetcher.Fetcher`. No Playwright in v0.3 (Q26).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from selectolax.parser import HTMLParser

from leadscraper.crawler.fetcher import SKIP_NETWORK, FetchResult, Fetcher
from leadscraper.crawler.website import Website, WebsiteRejected, normalize_website
from leadscraper.services.resolver.geo import norm

_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "#", "sms:", "fax:")


@dataclass(slots=True)
class Page:
    url: str
    html: str
    kind: str                         # "home" | "legal" | "contact" | "fallback"
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


def find_contact_links(html: str, base_url: str, keywords: Sequence[str]) -> list[tuple[str, str]]:
    """``[(absolute_url, matched_keyword)]`` on the same registered domain, ordered by keyword
    priority (config order: legal notice first) and then document order; duplicates removed."""
    base = normalize_website(base_url)
    hits: list[tuple[int, int, str, str]] = []
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
        path = unquote(urlsplit(target.url).path).replace("/", " ")
        idx = _match(f"{a.text(separator=' ')} {path}", keywords)
        if idx is not None:
            hits.append((idx, pos, target.url, keywords[idx]))
    out: list[tuple[str, str]] = []
    seen = {base.url.rstrip("/")}
    for _, _, url, kw in sorted(hits):
        key = url.split("#")[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append((url, kw))
    return out


def _is_legal(text: str, legal_markers: Sequence[str]) -> bool:
    return _match(text, legal_markers) is not None


def _save(save_dir: Path | None, url: str, html: str) -> None:
    if save_dir is None:
        return
    save_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"
    (save_dir / name).write_text(html, encoding="utf-8")


async def crawl_site(fetcher: Fetcher, website: Website, *, keywords: Sequence[str],
                     fallback_paths: Sequence[str], legal_markers: Sequence[str] = (),
                     save_dir: Path | None = None) -> SiteCrawl:
    """Crawl one company website: homepage + matching legal/contact pages (≤ page limit)."""
    home: FetchResult = await fetcher.fetch(website)
    unreachable = home.skipped == SKIP_NETWORK or website.origin in fetcher.unreachable_origins
    if not home.ok and website.url.startswith("https://") and unreachable:     # no TLS → try http
        home = await fetcher.fetch(normalize_website("http://" + website.url[len("https://"):]))
    if not home.ok or home.final_site is None:
        return SiteCrawl(website=website, skipped=home.skipped or "no_html")
    final = home.final_site
    crawl = SiteCrawl(website=final, lang=html_lang(home.html or ""))
    crawl.pages.append(Page(final.url, home.html or "", "home"))
    _save(save_dir, final.url, home.html or "")

    links = find_contact_links(home.html or "", final.url, keywords)
    candidates: list[tuple[str, str, bool]] = [
        (url, "legal" if _is_legal(kw, legal_markers) else "contact", _is_legal(kw, legal_markers))
        for url, kw in links]
    if not candidates:
        candidates = [(urljoin(final.origin + "/", p.lstrip("/")), "fallback",
                       _is_legal(p, legal_markers)) for p in fallback_paths]
    fetched = {final.url.rstrip("/")}
    for url, kind, is_legal in candidates:
        if fetcher.pages_left(final.registered_domain) <= 0:
            break
        if url.rstrip("/") in fetched:
            continue
        res = await fetcher.fetch(url)
        fetched.add(url.rstrip("/"))
        if res.ok and res.final_site is not None:
            if res.final_site.registered_domain != final.registered_domain:
                continue                       # redirected off-site: not the company's page
            crawl.pages.append(Page(res.final_site.url, res.html or "", kind, is_legal))
            _save(save_dir, res.final_site.url, res.html or "")
    return crawl
