"""Sitemap parsing for the contact-page fallback."""

from __future__ import annotations

import html
import re

from leadscraper import constants as C

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_INDEX = re.compile(r"<sitemapindex\b", re.IGNORECASE)


def parse_locs(text: str, limit: int = C.SITEMAP_MAX_LOCS) -> list[str]:
    """The ``<loc>`` URLs in document order (entities decoded), at most ``limit``."""
    out: list[str] = []
    for m in _LOC.finditer(text or ""):
        out.append(html.unescape(m.group(1)))
        if len(out) >= limit:
            break
    return out


def is_index(text: str) -> bool:
    """A ``<sitemapindex>`` (its locs are child sitemaps, not pages)."""
    return bool(_INDEX.search(text or ""))


def is_gzip(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(".gz")


def pick_children(locs: list[str], limit: int = C.SITEMAP_MAX_CHILDREN) -> list[str]:
    """Child sitemaps to read from an index: URLs containing "page" first (WordPress/Yoast style
    ``page-sitemap.xml``), document order otherwise, ``.gz`` skipped."""
    usable = [u for u in locs if not is_gzip(u)]
    return sorted(usable, key=lambda u: "page" not in u.lower())[:limit]
