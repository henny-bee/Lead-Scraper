import time
from collections.abc import Iterator
from pathlib import Path

import dns.name
import dns.resolver
import pytest
from fastapi.testclient import TestClient

from leadscraper.api.deps import RateLimiter
from leadscraper.domain.models import JobStatus
from leadscraper.main import create_app
from leadscraper.services.verify_service import EmailVerifier
from leadscraper.settings import load_settings
from leadscraper.verification import lists
from leadscraper.verification.dns import DnsChecker

A24_RESULT_KEYS = {"email", "result", "reason", "score", "verification_level", "checks", "cached",
                   "checked_at"}


class MX:
    def __init__(self, host: str) -> None:
        self.preference, self.exchange = 10, dns.name.from_text(host)


class FakeResolver:
    async def resolve(self, qname, rdtype, **_kw):
        if qname == "gone-example.de":
            raise dns.resolver.NXDOMAIN()
        return [MX(f"mx01.{qname}.")]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs")}))
    app.state.verifier_factory = lambda settings: EmailVerifier(
        settings, dns=DnsChecker(FakeResolver()),
        suppression=lists.SuppressionList(path=tmp_path / "none.txt"))
    app.state.rate_limiter = RateLimiter(per_minute=1e6, burst=10**6)   # polling loops below
    with TestClient(app) as c:
        yield c


def wait_done(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        body = client.get(f"/verify/{job_id}").json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.02)
    raise AssertionError("verify job did not finish")


def test_sync_up_to_50(client: TestClient) -> None:
    resp = client.post("/verify", json={"emails": ["info@example.de", "vertrieb@example-logistik.de"]})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"status", "count", "results"} and body["status"] == "success"
    assert body["count"] == 2
    first = body["results"][0]
    assert set(first) == A24_RESULT_KEYS
    assert first["email"] == "info@example.de" and first["result"] == "unknown"
    assert first["verification_level"] == "dns" and first["checks"]["domain_has_mx"] is True
    assert first["checks"]["mx_hosts"] == ["mx01.example.de"]
    assert first["cached"] is False and first["checked_at"].endswith("Z")


def test_sync_boundary_50_and_mixed_results(client: TestClient) -> None:
    emails = [f"user{i}@example.de" for i in range(48)] + ["kaputt", "x@gone-example.de"]
    body = client.post("/verify", json={"emails": emails}).json()
    assert body["count"] == 50
    assert body["results"][48]["result"] == "undeliverable" and body["results"][48]["reason"] == "invalid_syntax"
    assert body["results"][49]["reason"] == "domain_not_found"


def test_smtp_check_with_smtp_disabled_warns(client: TestClient) -> None:
    body = client.post("/verify", json={"emails": ["info@example.de"], "smtp_check": True}).json()
    assert body["results"][0]["result"] == "unknown" and body["results"][0]["reason"] == "smtp_disabled"
    assert body["warnings"] and "SMTP" in body["warnings"][0]


def test_51_becomes_async_job_and_repeatable_read(client: TestClient) -> None:
    emails = [f"user{i}@example.de" for i in range(51)]
    resp = client.post("/verify", json={"emails": emails})
    assert resp.status_code == 202
    queued = resp.json()
    assert set(queued) == {"status", "job_id", "poll_url"} and queued["status"] == "queued"
    job_id = queued["job_id"]
    assert job_id.startswith("vrf_") and queued["poll_url"] == f"/verify/{job_id}"
    assert client.post("/verify", json={"emails": emails}).json()["job_id"] == job_id   # idempotent
    final = wait_done(client, job_id)
    assert final["status"] == "success" and final["job_id"] == job_id and final["count"] == 51
    assert all(set(r) == A24_RESULT_KEYS for r in final["results"])
    jobs = client.app.state.jobs
    assert client.get(f"/verify/{job_id}").json() == final                   # T28: repeatable read
    assert client.get(f"/verify/{job_id}").status_code == 200
    assert jobs.get(job_id) is not None and (Path(jobs.temp_root) / job_id).exists()


def test_running_job_shape(client: TestClient) -> None:
    emails = [f"u{i}@example.de" for i in range(60)]
    jobs = client.app.state.jobs
    job, _ = jobs.create("verify", {"emails": emails, "smtp_check": False}, target=60)
    body = client.get(f"/verify/{job.job_id}").json()
    assert body == {"status": "queued", "job_id": job.job_id, "count": 0,
                    "progress": {"target": 60, "checked": 0}}


def test_over_10000_rejected(client: TestClient) -> None:
    resp = client.post("/verify", json={"emails": ["a@b-example.de"] * 10_001})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


def test_unknown_and_scrape_job_ids_404(client: TestClient) -> None:
    assert client.get("/verify/vrf_00000000000000000000000000").status_code == 404
    scrape, _ = client.app.state.jobs.create("scrape", {"x": 1})
    resp = client.get(f"/verify/{scrape.job_id}")
    assert resp.status_code == 404 and resp.json()["status"] == "error"


def test_failed_verify_job_body_and_ttl(client: TestClient) -> None:
    jobs = client.app.state.jobs
    job, _ = jobs.create("verify", {"emails": ["x"] * 60})
    jobs.fail(job.job_id, "internal_error", "boom")
    body = client.get(f"/verify/{job.job_id}").json()
    assert body == {"status": "failed", "job_id": job.job_id,
                    "error": {"code": "internal_error", "message": "boom", "details": {}}}
    assert jobs.get(job.job_id) is not None                   # failed jobs follow the failed-TTL


def test_verify_repost_returns_current_status(client: TestClient) -> None:
    """D4 follow-up: an identical re-POST points at the same job and reports its current status."""
    emails = [f"re{i}@example.de" for i in range(55)]
    jobs = client.app.state.jobs
    job, _ = jobs.create("verify", {"emails": emails, "smtp_check": False}, target=55)
    jobs.set_status(job.job_id, JobStatus.RUNNING)
    body = client.post("/verify", json={"emails": emails}).json()
    assert body["job_id"] == job.job_id and body["status"] == "running"
