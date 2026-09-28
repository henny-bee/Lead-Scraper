"""Website resolver (ARCHITECTURE.md §3.4 "website" row, §3.8, §4; PLAN.md Q21, Q22).

For each candidate: take the website from the source (OSM ``website``/``contact:website``),
normalise it (scheme added, host lower-cased, default port / fragment / tracking parameters
removed), reject non-http(s), invalid, IP-literal-private and social/directory-only targets, and
compute the registered domain (tldextract ``top_domain_under_public_suffix``, bundled PSL, no
network). During crawl the redirect chain is followed hop by hop by
:meth:`leadscraper.crawler.fetcher.Fetcher.fetch` (the only code that sends crawl requests, so C11
politeness always applies) — every hop is checked against private/loopback/link-local/reserved
addresses (SSRF, Q21) — and the final origin becomes the company's ``website``. Candidates without a usable website are skipped in core mode (A§4).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

from leadscraper import constants as C
from leadscraper.domain.models import CompanyCandidate

_TLD = tldextract.TLDExtract(suffix_list_urls=())   # bundled PSL snapshot, no network call
_DEFAULT_PORTS = {"http": 80, "https": 443}
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

Resolve = Callable[[str], Awaitable[list[str]]]


class WebsiteRejected(Exception):
    """A URL that must not be crawled; ``reason`` is one of the SKIP_* values."""

    def __init__(self, reason: str, url: str | None = None) -> None:
        super().__init__(f"{reason}: {url}")
        self.reason, self.url = reason, url


SKIP_NO_WEBSITE = "no_website"
SKIP_INVALID = "invalid_url"
SKIP_SOCIAL = "social_or_directory"
SKIP_PRIVATE = "non_public_address"
SKIP_REDIRECTS = "too_many_redirects"


@dataclass(slots=True, frozen=True)
class Website:
    url: str                     # normalised URL to start crawling from
    origin: str                  # scheme://host[:port] — stored as the company's `website`
    host: str
    registered_domain: str


def registered_domain(url: str | None) -> str | None:
    """``https://www.shop.firma-example.de/x`` → ``firma-example.de``; ``None`` for IPs / invalid hosts."""
    if not url or not url.strip():
        return None
    raw = url.strip()
    if "//" not in raw:
        raw = "//" + raw
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        return None
    if not host:
        return None
    return _TLD(host.rstrip(".").lower()).top_domain_under_public_suffix or None


def _is_non_company_site(domain: str) -> bool:
    if domain in C.NON_COMPANY_SITE_DOMAINS:
        return True
    label = domain.split(".", 1)[0] + "."
    return label in C.NON_COMPANY_SITE_DOMAINS          # "yelp." matches yelp.de, yelp.co.uk …


def _clean_query(query: str) -> str:
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if not k.lower().startswith(C.TRACKING_QUERY_PREFIXES)
            and k.lower() not in C.TRACKING_QUERY_KEYS]
    return urlencode(kept)


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_public_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def normalize_website(raw: str | None) -> Website:
    """Normalise a source website value or raise :class:`WebsiteRejected`."""
    if not raw or not raw.strip():
        raise WebsiteRejected(SKIP_NO_WEBSITE)
    value = raw.strip().split(";")[0].split(" ")[0].strip()      # OSM: "a-example.de;b-example.de" → first
    if "://" not in value:
        if value.startswith("//"):
            value = "https:" + value
        elif ":" in value.split("/")[0] and not value.split("/")[0].split(":")[-1].isdigit():
            raise WebsiteRejected(SKIP_INVALID, raw)           # mailto:, tel:, javascript: …
        else:
            value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise WebsiteRejected(SKIP_INVALID, raw) from exc
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise WebsiteRejected(SKIP_INVALID, raw)
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise WebsiteRejected(SKIP_INVALID, raw)
    try:
        host = host.encode("idna").decode("ascii") if not host.isascii() else host
    except UnicodeError as exc:
        raise WebsiteRejected(SKIP_INVALID, raw) from exc
    ip = _ip_literal(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise WebsiteRejected(SKIP_PRIVATE, raw)
        domain = None
    else:
        domain = _TLD(host).top_domain_under_public_suffix or None
        if not domain:
            raise WebsiteRejected(SKIP_INVALID, raw)            # "localhost", "intranet", bad TLD
        if _is_non_company_site(domain):
            raise WebsiteRejected(SKIP_SOCIAL, raw)
    netloc = host if ip is None or ip.version == 4 else f"[{host}]"
    if port and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    url = urlunsplit((scheme, netloc, path, _clean_query(parts.query), ""))
    return Website(url=url, origin=f"{scheme}://{netloc}", host=host,
                   registered_domain=domain or host)


def website_for(candidate: CompanyCandidate) -> Website:
    """The candidate's usable website (source tags), or :class:`WebsiteRejected`."""
    return normalize_website(candidate.website)


# --- SSRF-safe host check + redirect confirmation --------------------------------------------------
async def system_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


class HostGuard:
    """Checks that a host resolves only to public addresses (Q21). Per-job cache only (C7).

    Known limitation (accepted for v0.3, Supervisor T13 review): this lookup and the one httpx
    performs when connecting are separate, so DNS rebinding (a host answering with a public
    address here and a private one at connect time) is not prevented.
    """

    def __init__(self, resolve: Resolve = system_resolve) -> None:
        self._resolve = resolve
        self._cache: dict[str, bool] = {}

    async def is_public(self, host: str) -> bool:
        host = host.lower().strip("[]")
        if host in self._cache:
            return self._cache[host]
        ip = _ip_literal(host)
        if ip is not None:
            ok = is_public_ip(ip)
        else:
            try:
                addrs = await self._resolve(host)
            except (OSError, UnicodeError):
                addrs = []
            ok = bool(addrs) and all(is_public_ip(a) for a in addrs)
        self._cache[host] = ok
        return ok

    async def check(self, site: Website) -> None:
        if not await self.is_public(site.host):
            raise WebsiteRejected(SKIP_PRIVATE, site.url)
