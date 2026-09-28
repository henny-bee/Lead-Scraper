"""Optional API-key auth and in-process per-IP rate limiting (ARCHITECTURE.md §2, §2.3, §9; Q6).

- ``API_KEY`` empty (default) → no authentication (A§9). Set → every endpoint except ``/health``
  requires ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``, compared with
  ``secrets.compare_digest``; missing → ``401 unauthorized``, wrong → ``403 forbidden``.
- Per-IP token bucket (no Redis, A§2): ``RATE_LIMIT_REQUESTS_PER_MINUTE`` refill with
  ``RATE_LIMIT_BURST`` capacity → ``429 rate_limited`` + ``Retry-After``. State is bounded: idle
  IPs are evicted after ``RATE_LIMIT_IDLE_EVICT_S`` and at most ``RATE_LIMIT_MAX_TRACKED_IPS`` are
  tracked (least recently seen dropped first). The client IP is the socket peer address; proxy
  headers are not trusted (run behind a proxy → configure the proxy's own limits).
"""

from __future__ import annotations

import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from leadscraper import constants as C
from leadscraper.api.errors import ApiError


def _presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


async def require_api_key(request: Request) -> None:
    expected: str = request.app.state.settings.api_key
    if not expected:
        return                                           # zero-config: auth off (A§9)
    presented = _presented_key(request)
    if presented is None:
        raise ApiError(401, "unauthorized", "API key required (X-API-Key or Authorization: Bearer)",
                       headers={"WWW-Authenticate": "Bearer"})
    if not secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise ApiError(403, "forbidden", "Invalid API key")


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token bucket per client IP with bounded state."""

    def __init__(self, *, per_minute: float = C.RATE_LIMIT_REQUESTS_PER_MINUTE,
                 burst: int = C.RATE_LIMIT_BURST, max_ips: int = C.RATE_LIMIT_MAX_TRACKED_IPS,
                 idle_evict_s: float = C.RATE_LIMIT_IDLE_EVICT_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(burst)
        self.max_ips = max_ips
        self.idle_evict_s = idle_evict_s
        self.clock = clock
        self.buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def _evict(self, now: float) -> None:
        while self.buckets:
            ip, bucket = next(iter(self.buckets.items()))
            if now - bucket.updated >= self.idle_evict_s or len(self.buckets) > self.max_ips:
                del self.buckets[ip]
            else:
                break

    def check(self, ip: str) -> float | None:
        """Consume one token; returns None if allowed, else seconds until the next token."""
        now = self.clock()
        bucket = self.buckets.pop(ip, None)
        if bucket is None:
            bucket = _Bucket(self.burst, now)
        else:
            bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
        self.buckets[ip] = bucket                        # most recently seen at the end
        self._evict(now)
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        return (1 - bucket.tokens) / self.rate if self.rate > 0 else float(self.idle_evict_s)


async def rate_limit(request: Request) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    ip = request.client.host if request.client else "unknown"
    retry_after = limiter.check(ip)
    if retry_after is not None:
        seconds = max(1, math.ceil(retry_after))
        raise ApiError(429, "rate_limited", "Too many requests", {"retry_after_s": seconds},
                       headers={"Retry-After": str(seconds)})
