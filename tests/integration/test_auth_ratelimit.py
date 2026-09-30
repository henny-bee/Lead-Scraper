"""Optional API key and in-process per-IP rate limiting."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from leadscraper import constants as C
from leadscraper.api.deps import RateLimiter
from leadscraper.main import create_app
from leadscraper.settings import load_settings

RESOLVE = {"country": "Germany", "regions": ["Bayern"], "industries": ["Logistik"],
           "information": ["company_email"]}


def client_for(tmp_path: Path, **env: str) -> TestClient:
    return TestClient(create_app(load_settings({"TEMP_DIR": str(tmp_path), **env})))


def assert_envelope(resp, status: int, code: str) -> None:
    assert resp.status_code == status
    body = resp.json()
    assert body["status"] == "error" and body["error"]["code"] == code
    assert set(body["error"]) == {"code", "message", "details"}


def test_no_api_key_configured_means_no_auth(tmp_path: Path) -> None:
    with client_for(tmp_path) as c:
        assert c.get("/meta/industries", params={"q": "logistik"}).status_code == 200
        assert c.post("/scrape/resolve", json=RESOLVE).status_code == 200


@pytest.mark.parametrize("path,method", [("/scrape/resolve", "post"), ("/meta/countries", "get"),
                                         ("/verify/vrf_00000000000000000000000000", "get"),
                                         ("/scrape/scr_00000000000000000000000000", "get")])
def test_api_key_required_everywhere_except_health(tmp_path: Path, path: str, method: str) -> None:
    with client_for(tmp_path, API_KEY="s3cret-key") as c:
        call = getattr(c, method)
        kwargs = {"json": RESOLVE} if method == "post" else {}
        missing = call(path, **kwargs)
        assert_envelope(missing, 401, "unauthorized")
        assert missing.headers["www-authenticate"] == "Bearer"
        assert_envelope(call(path, headers={"X-API-Key": "wrong"}, **kwargs), 403, "forbidden")
        assert_envelope(call(path, headers={"Authorization": "Bearer nope"}, **kwargs), 403, "forbidden")
        ok_status = {"post": 200, "get": None}[method]
        good = call(path, headers={"X-API-Key": "s3cret-key"}, **kwargs)
        assert good.status_code not in (401, 403)
        if ok_status:
            assert good.status_code == ok_status
        bearer = call(path, headers={"Authorization": "Bearer s3cret-key"}, **kwargs)
        assert bearer.status_code not in (401, 403)
        assert c.get("/health").status_code == 200                    # /health stays open


def test_malformed_authorization_header_is_missing_key(tmp_path: Path) -> None:
    with client_for(tmp_path, API_KEY="k") as c:
        assert_envelope(c.get("/meta/countries", headers={"Authorization": "Basic k"}), 401, "unauthorized")


def test_burst_beyond_limit_returns_429_with_retry_after(tmp_path: Path) -> None:
    with client_for(tmp_path) as c:
        statuses = [c.get("/meta/countries").status_code for _ in range(C.RATE_LIMIT_BURST)]
        assert set(statuses) == {200}
        limited = c.get("/meta/countries")
        assert_envelope(limited, 429, "rate_limited")
        assert int(limited.headers["retry-after"]) >= 1
        assert limited.json()["error"]["details"]["retry_after_s"] >= 1
        assert c.get("/health").status_code == 200                    # health not rate-limited


def test_bucket_refills_over_time() -> None:
    now = [0.0]
    limiter = RateLimiter(per_minute=60, burst=2, clock=lambda: now[0])
    assert limiter.check("1.1.1.1") is None and limiter.check("1.1.1.1") is None
    wait = limiter.check("1.1.1.1")
    assert wait == pytest.approx(1.0)
    now[0] += 1.0
    assert limiter.check("1.1.1.1") is None
    assert limiter.check("2.2.2.2") is None                           # per IP


def test_limiter_state_is_bounded() -> None:
    now = [0.0]
    limiter = RateLimiter(per_minute=60, burst=5, max_ips=100, idle_evict_s=600, clock=lambda: now[0])
    for i in range(1000):
        limiter.check(f"10.0.{i // 256}.{i % 256}")
        now[0] += 0.001
    assert len(limiter.buckets) == 100                                # LRU cap
    assert "10.0.3.231" in limiter.buckets                            # most recent kept
    now[0] += 601
    limiter.check("192.0.2.1")
    assert list(limiter.buckets) == ["192.0.2.1"]                     # idle IPs evicted


def test_default_limiter_uses_constants() -> None:
    limiter = RateLimiter()
    assert limiter.burst == C.RATE_LIMIT_BURST == 30
    assert limiter.rate == pytest.approx(C.RATE_LIMIT_REQUESTS_PER_MINUTE / 60)
    assert limiter.max_ips == C.RATE_LIMIT_MAX_TRACKED_IPS == 10_000
