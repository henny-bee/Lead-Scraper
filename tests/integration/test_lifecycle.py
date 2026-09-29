"""Full job lifecycle, TTL, zero-config boot and "no cross-job data"."""

import gc
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from leadscraper.api.deps import RateLimiter
from leadscraper.jobs.cleanup import sweep_once
from leadscraper.jobs.manager import JobManager
from leadscraper.main import create_app
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import OverpassGate

OVERPASS = "https://overpass.test/api/interpreter"
HTML = {"content-type": "text/html; charset=utf-8"}
EMAIL = "info@lifecycle-firma-example.de"
BODY = {"country": "Germany", "regions": ["NRW"], "industries": ["Maschinenbau"],
        "information": ["company_name", "company_email", "website"], "max_output": 5}


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/":
        return httpx.Response(200, headers=HTML,
                              text=f"<html><p>Lifecycle Firma GmbH</p><p>{EMAIL}</p></html>")
    return httpx.Response(404, headers=HTML, text="x")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": [
            {"type": "node", "id": 7, "lat": 51.2, "lon": 6.8,
             "tags": {"name": "Lifecycle Firma", "website": "https://lifecycle-firma-example.de"}}]}))
        mock.get(url__regex=r"https://lifecycle-firma-example\.de/.*").mock(side_effect=site)
        app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "OVERPASS_URL": OVERPASS, "WEB_SEARCH_URL": "off",
                                        "CRAWLER_PER_DOMAIN_DELAY_S": "0", "JOB_TTL_MINUTES": "0.002"}))

        async def public(_host: str) -> list[str]:
            return ["93.184.216.34"]

        app.state.pipeline_deps.dns_resolve = public
        app.state.pipeline_deps.gate = OverpassGate(per_minute=600_000)
        app.state.rate_limiter = RateLimiter(per_minute=1e6, burst=10**6)
        with TestClient(app) as c:
            yield c


def poll_until_final(client: TestClient, job_id: str) -> dict:
    for _ in range(300):
        body = client.get(f"/scrape/{job_id}").json()
        if body["status"] not in ("queued", "running"):
            return body
        assert set(body) == {"status", "job_id", "count", "progress", "resolved"}
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_full_lifecycle_post_poll_get_export_delete(client: TestClient) -> None:
    """Flow, (reads never delete): poll → final GET (repeatable) → export → DELETE."""
    jobs: JobManager = client.app.state.jobs
    job_id = client.post("/scrape", json=BODY).json()["job_id"]
    temp = Path(jobs.temp_root) / job_id
    final = poll_until_final(client, job_id)
    assert final["status"] == "success" and final["companies"][0]["company_email"] == EMAIL
    assert client.get(f"/scrape/{job_id}").json() == final       # still readable
    assert client.get(f"/scrape/{job_id}/export").status_code == 200
    assert temp.exists()
    assert client.delete(f"/scrape/{job_id}").status_code == 204
    assert not temp.exists() and jobs.get(job_id) is None
    assert client.get(f"/scrape/{job_id}").status_code == 404
    assert list(Path(jobs.temp_root).iterdir()) == []


def test_lifecycle_with_explicit_delete(client: TestClient) -> None:
    jobs: JobManager = client.app.state.jobs
    job_id = client.post("/scrape", json=BODY).json()["job_id"]
    for _ in range(300):
        if jobs.get(job_id).status.is_terminal:
            break
        time.sleep(0.02)
    page = client.get(f"/scrape/{job_id}", params={"offset": 0, "limit": 1}).json()
    assert page["count"] == 1                                     # single page = last page
    assert client.delete(f"/scrape/{job_id}").status_code == 204
    assert list(Path(jobs.temp_root).iterdir()) == []


def test_ttl_removes_unfetched_job(client: TestClient) -> None:
    jobs: JobManager = client.app.state.jobs
    job_id = client.post("/scrape", json=BODY).json()["job_id"]
    for _ in range(300):
        if jobs.get(job_id).status.is_terminal:
            break
        time.sleep(0.02)
    assert client.get(f"/scrape/{job_id}").status_code == 200     # a read does not stop the TTL
    time.sleep(0.2)                                               # > JOB_TTL_MINUTES (0.12 s)
    client.portal.call(sweep_once, jobs, client.app.state.settings)
    assert jobs.get(job_id) is None and not (Path(jobs.temp_root) / job_id).exists()
    assert client.get(f"/scrape/{job_id}").status_code == 404


def _iter_module_values():
    for name, module in list(sys.modules.items()):
        if name.startswith("leadscraper") and module is not None:
            yield from vars(module).values()


def _contains(value, needle: str, depth: int = 0) -> bool:
    if depth > 3:
        return False
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains(k, needle, depth + 1) or _contains(v, needle, depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains(v, needle, depth + 1) for v in value)
    return False


def test_no_cross_job_data_after_delivery(client: TestClient) -> None:
    from leadscraper.crawler.fetcher import Fetcher
    from leadscraper.services.dedup import Deduplicator
    from leadscraper.services.scrape_service import ScrapePipeline
    from leadscraper.verification.dns import DnsChecker

    jobs: JobManager = client.app.state.jobs
    job_id = client.post("/scrape", json={**BODY, "verify_emails": False}).json()["job_id"]
    final = poll_until_final(client, job_id)
    assert final["companies"][0]["company_email"] == EMAIL
    del final
    # reading keeps the job; its data goes when the client deletes it (or by TTL)
    assert client.delete(f"/scrape/{job_id}").status_code == 204
    # JobManager holds only a data-free tombstone
    assert jobs.jobs == {} and list(jobs.tombstones) == [job_id]
    assert all(isinstance(v, float) for v in jobs.tombstones.values())
    assert jobs._by_hash == {}
    # TEMP_DIR has nothing for the job
    assert list(Path(jobs.temp_root).iterdir()) == []
    # no per-job object (pipeline, fetcher/robots/DNS caches, dedup state) survives the job
    gc.collect()
    leaked = [type(o).__name__ for o in gc.get_objects()
              if isinstance(o, (ScrapePipeline, Fetcher, Deduplicator, DnsChecker))]
    assert leaked == []
    # and no module-level structure references the company's email
    assert not any(_contains(v, EMAIL) for v in _iter_module_values())


def test_zero_config_boot_no_outbound_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty environment, outbound sockets blocked except loopback: the app boots, is ready and
    resolves requests without any network call."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    attempts: list = []

    def connect(self, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        attempts.append(address)
        raise OSError("outbound network blocked in test")

    def getaddrinfo(host, *args, **kwargs):
        if host in ("127.0.0.1", "::1", "localhost", None, "testserver"):
            return real_getaddrinfo(host, *args, **kwargs)
        attempts.append(host)
        raise OSError("DNS blocked in test")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    settings = load_settings({})
    assert settings == load_settings({}) and settings.api_key == "" and settings.nominatim_url == ""
    app = create_app(settings)
    app.state.jobs = JobManager(tmp_path)          # only the temp location differs from defaults
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        body = c.post("/scrape/resolve", json={"country": "Deutschland", "regions": ["NRW"],
                                               "industries": ["Maschinenbau"],
                                               "information": ["website"]}).json()
        assert body["resolved"]["country"]["code"] == "DE"
        assert c.get("/metrics").status_code == 200
    assert attempts == []
