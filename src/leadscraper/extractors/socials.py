"""Social profile links for ``GET /contacts`` (ported from adopth/website-email-contact-scraper,
``extract_socials.py``, simplified onto selectolax)."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import parse_qsl, urlsplit

from selectolax.parser import HTMLParser

from leadscraper.extractors.social_regexes import PLATFORM_REGEXES_GLOBAL

PLATFORMS: tuple[str, ...] = tuple(PLATFORM_REGEXES_GLOBAL)
MAX_HTML_SCAN_CHARS = 1_500_000
ANCHOR_ONLY = frozenset({"githubs"})
#: Site-builder vendors whose own profiles leak in via "Built with X" footers.
VENDOR_HANDLES = frozenset(
    "wix wixstudio shopify shopifyplus wordpress wordpressdotcom squarespace godaddy webflow "
    "elementor woocommerce weebly jimdo duda strikingly bigcommerce prestashop".split())
#: First path segments that are never a profile (share dialogs, logins, content pages, …).
_DROP_FIRST = frozenset(
    "share sharer sharer.php share.php intent dialog login signup plugins hashtag search explore "
    "watch p reel reels tv stories groups events policies help settings about privacy terms "
    "legal accounts home i oauth widgets embed".split())
_KEEP_QUERY = {"facebooks": "id", "whatsapps": "phone"}
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


def _keep(platform: str, segs: list[str]) -> list[str] | None:
    """The profile part of the path, or ``None`` when the URL is not a profile."""
    first = segs[0].lower() if segs else ""
    two = len(segs) >= 2
    if platform == "linkedins":
        return segs[:2] if two and first in ("in", "company", "school") else None
    if platform == "facebooks":
        if first in ("people", "pages"):
            return segs[:3] if len(segs) >= 3 and segs[2].isdigit() else None
        return segs[:1]
    if platform == "youtubes":
        if first.startswith("@"):
            return segs[:1]
        return segs[:2] if two and first in ("channel", "c", "user") else None
    if platform in ("snapchats", "blueskys", "discords"):
        return segs[:2] if two and first in ("add", "profile", "invite") else segs[:1]
    if platform == "reddits":
        return segs[:2] if two and first in ("r", "u", "user") else None
    if platform == "telegrams":
        if first == "s" and two:
            return segs[1:2]
        return segs[:2] if two and first == "joinchat" else segs[:1]
    if platform == "pinterests":
        return None if first == "pin" else segs[:1]
    if platform == "whatsapps":
        return segs[-1:] if segs and first != "send" else []
    if platform == "tiktoks":
        return segs[:1] if first.startswith("@") else None
    return segs[:1]                      # twitters, instagrams, threads, githubs, calendlys, mediums


def canonical(platform: str, raw: str) -> str | None:
    """``https://<host>/<path>`` for a profile URL, or ``None`` for non-profile URLs."""
    url = html_lib.unescape(raw.strip())
    if not _SCHEME.match(url):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix) and len(host) > len(prefix):
            host = host[len(prefix):]
    if host == "twitter.com":
        host = "x.com"
    segs = [s for s in parts.path.split("/") if s]
    if platform == "youtubes" and host == "youtu.be":
        return None                                  # always a video, never a channel
    if platform == "mediums" and host.endswith(".medium.com"):
        return f"https://{host}"                     # <name>.medium.com publication
    if segs and segs[0].lower() in _DROP_FIRST and segs[0] != "profile.php":
        return None
    query = ""
    keep_param = _KEEP_QUERY.get(platform)
    if keep_param:
        value = next((v for k, v in parse_qsl(parts.query) if k == keep_param), None)
        if segs[:1] == ["profile.php"] or (platform == "whatsapps" and not segs[1:] and segs[:1] == ["send"]):
            if not value:
                return None
            if platform == "whatsapps":
                return f"https://wa.me/{value.lstrip('+')}"
            query = f"?{keep_param}={value}"
    if segs[:1] == ["profile.php"]:
        return f"https://{host}/profile.php{query}"
    kept = _keep(platform, segs)
    if kept is None or (not kept and not query):
        return None
    if kept and kept[-1].lstrip("@").lower() in VENDOR_HANDLES:
        return None
    path = "/".join(kept)
    return f"https://{host}/{path}{query}" if path else f"https://{host}/{query}"


def page_socials(html: str) -> dict[str, list[tuple[str, bool]]]:
    """``{platform: [(canonical_url, from_href), …]}`` for one page, first occurrence order."""
    out: dict[str, dict[str, bool]] = {p: {} for p in PLATFORMS}
    tree = HTMLParser(html)
    hrefs = [a.attributes.get("href") or "" for a in tree.css("a[href]")]
    for platform, regex in PLATFORM_REGEXES_GLOBAL.items():
        for href in hrefs:
            for m in regex.finditer(href):
                if (url := canonical(platform, m.group(0))) is not None:
                    out[platform][url] = True
        if platform in ANCHOR_ONLY:
            continue
        for m in regex.finditer(html[:MAX_HTML_SCAN_CHARS]):
            if (url := canonical(platform, m.group(0))) is not None:
                out[platform].setdefault(url, False)
    return {p: list(v.items()) for p, v in out.items()}
