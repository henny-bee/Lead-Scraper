"""Step 4 — DNS: MX via dnspython async, fallback A/AAAA (implicit MX, RFC 5321 5.1), null-MX
detection (``0.``, RFC 7505)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

import dns.exception
import dns.resolver

from leadscraper import constants as C


class AsyncResolver(Protocol):
    async def resolve(self, qname: str, rdtype: str, **kwargs: Any) -> Any: ...


@dataclass(slots=True, frozen=True)
class DnsResult:
    status: str                         # mx | a_fallback | null_mx | no_mx | nxdomain | error
    mx_hosts: tuple[str, ...] = ()      # by preference, lowest first

    @property
    def has_mx(self) -> bool:
        return self.status == "mx"

    @property
    def accepts_mail(self) -> bool:
        return self.status in ("mx", "a_fallback")


def default_resolver() -> AsyncResolver:
    import dns.asyncresolver

    resolver = dns.asyncresolver.Resolver()
    resolver.timeout, resolver.lifetime = C.DNS_TIMEOUT_S, C.DNS_LIFETIME_S
    return resolver


@dataclass
class DnsChecker:
    resolver: AsyncResolver | None = None
    cache: dict[str, DnsResult] = field(default_factory=dict)       # job lifetime only
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def check(self, domain: str) -> DnsResult:
        domain = domain.lower().rstrip(".")
        if domain not in self.cache:
            async with self._locks.setdefault(domain, asyncio.Lock()):
                if domain not in self.cache:
                    self.cache[domain] = await self._lookup(domain)
        return self.cache[domain]

    async def _lookup(self, domain: str) -> DnsResult:
        if self.resolver is None:
            self.resolver = default_resolver()
        resolver = self.resolver
        try:
            answer = await resolver.resolve(domain, "MX")
            records = sorted((int(r.preference), str(r.exchange).rstrip(".").lower()) for r in answer)
            hosts = tuple(host for _, host in records if host)
            if not hosts:                              # "0 ." → null MX: domain accepts no mail
                return DnsResult("null_mx")
            return DnsResult("mx", hosts)
        except dns.resolver.NXDOMAIN:
            return DnsResult("nxdomain")
        except dns.resolver.NoAnswer:
            pass
        except dns.exception.DNSException:            # timeout, SERVFAIL, no nameservers …
            return DnsResult("error")
        for rdtype in ("A", "AAAA"):                   # implicit MX
            try:
                if await resolver.resolve(domain, rdtype):
                    return DnsResult("a_fallback", (domain,))
            except dns.resolver.NXDOMAIN:
                return DnsResult("nxdomain")
            except dns.resolver.NoAnswer:
                continue
            except dns.exception.DNSException:
                return DnsResult("error")
        return DnsResult("no_mx")
