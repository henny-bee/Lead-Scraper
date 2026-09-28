"""Callback delivery (A§2.5, A§8, A§10.2; Q4) with respx — n8n-style internal URL allowed."""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from leadscraper.domain.models import JobStatus
from leadscraper.jobs.cleanup import sweep_once
from leadscraper.main import create_app
from leadscraper.services.scrape_service import runner_for
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import OverpassGate

OVERPASS = "https://overpass.test/api/interpreter"
CALLBACK = "http://n8n:5678/webhook/lead-result"
HTML = {"content-type": "text/html; charset=utf-8"}
BODY = {"country": "Germany", "regions": ["NRW", "Hessen"], "industries": ["Maschinenbau"],
        "information": ["company_name", "company_email", "website", "phone"], "max_output": 100,
        "verify_emails": False, "callback_url": CALLBACK}
ELEMENTS = [{"type": "node", "id": 1, "lat": 51.2, "lon": 6.8,
             "tags": {"name": "Firma Eins", "website": "https://firma-eins-example.de", "phone": "+49 211 123456"}}]


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/":
        return httpx.Response(200, headers=HTML, text="<html><p>Firma Eins GmbH</p><p>info@firma-eins-example.de</p></html>")
    return httpx.Response(404, headers=HTML, text="x")


def make_client(tmp_path: Path, mock: respx.MockRouter, **env: str) -> TestClient:
    mock.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": ELEMENTS}))
    mock.get(url__regex=r"https://firma-eins-example\.de/robots\.txt").mock(return_value=httpx.Response(404))
    mock.get(url__regex=r"https://firma-eins-example\.de/.*").mock(side_effect=site)
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "OVERPASS_URL": OVERPASS,
                                    "CRAWLER_PER_DOMAIN_DELAY_S": "0", **env}))

    async def public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    app.state.pipeline_deps.dns_resolve = public
    app.state.pipeline_deps.gate = OverpassGate(per_minute=600_000)
    return TestClient(app)


def wait_until(predicate, timeout: float = 6.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_callback_2xx_deletes_job(tmp_path: Path) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        hook = mock.post(CALLBACK).mock(return_value=httpx.Response(200))
        with make_client(tmp_path, mock) as client:
            job_id = client.post("/scrape", json=BODY).json()["job_id"]
            jobs = client.app.state.jobs
            wait_until(lambda: hook.called and jobs.get(job_id) is None)
            assert client.get(f"/scrape/{job_id}").status_code == 404
            assert not (Path(jobs.temp_root) / job_id).exists()
            assert jobs.is_tombstoned(job_id)
            assert client.delete(f"/scrape/{job_id}").status_code == 204
    sent = json.loads(hook.calls[0].request.content)
    assert sent["status"] == "success" and sent["job_id"] == job_id and sent["count"] == 1
    assert sent["companies"] == [{"company_name": "Firma Eins GmbH", "company_email": "info@firma-eins-example.de",
                                  "website": "https://firma-eins-example.de", "phone": "+49211123456",
                                  "country": "Germany", "region": "NRW", "industry": "Maschinenbau"}]


def test_callback_500_keeps_job_pollable_until_ttl(tmp_path: Path) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        hook = mock.post(CALLBACK).mock(return_value=httpx.Response(500))
        with make_client(tmp_path, mock, JOB_TTL_MINUTES="0.002") as client:
            jobs = client.app.state.jobs
            job_id = client.post("/scrape", json=BODY).json()["job_id"]
            wait_until(lambda: hook.called and jobs.get(job_id).status.is_terminal)
            job = jobs.get(job_id)
            assert job.status is JobStatus.SUCCESS                      # callback failure ≠ job failure
            time.sleep(0.2)                                            # > JOB_TTL_MINUTES (0.12 s)
            client.portal.call(sweep_once, jobs, client.app.state.settings)
            assert jobs.get(job_id) is None and not job.temp_dir.exists()
            assert client.get(f"/scrape/{job_id}").status_code == 404


def test_callback_timeout_keeps_job(tmp_path: Path) -> None:
    with respx.mock(assert_all_mocked=True) as mock:
        hook = mock.post(CALLBACK).mock(side_effect=httpx.ConnectTimeout("n8n down"))
        with make_client(tmp_path, mock) as client:
            jobs = client.app.state.jobs
            job_id = client.post("/scrape", json=BODY).json()["job_id"]
            wait_until(lambda: hook.called and jobs.get(job_id).status.is_terminal)
            body = client.get(f"/scrape/{job_id}").json()             # still pollable
            assert body["status"] == "success" and body["count"] == 1
    assert hook.call_count == 1                                        # no retry loop (v0.4)


def test_failed_job_also_calls_back(tmp_path: Path) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        hook = mock.post(CALLBACK).mock(return_value=httpx.Response(204))
        with make_client(tmp_path, mock) as client:
            jobs = client.app.state.jobs
            job, _ = jobs.create("scrape", {**BODY, "regions": ["Bayerm"]}, callback_url=CALLBACK)

            async def start() -> None:
                await jobs.start(job, runner_for(client.app.state.pipeline_deps, jobs))

            client.portal.call(start)
            assert jobs.get(job.job_id) is None                        # delivered → deleted
    sent = json.loads(hook.calls[0].request.content)
    assert sent["status"] == "failed" and sent["error"]["code"] == "unresolved_region"


def test_no_callback_without_url(tmp_path: Path) -> None:
    body = {k: v for k, v in BODY.items() if k != "callback_url"}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        hook = mock.post(url__regex=r"http://n8n.*").mock(return_value=httpx.Response(200))
        with make_client(tmp_path, mock) as client:
            jobs = client.app.state.jobs
            job_id = client.post("/scrape", json=body).json()["job_id"]
            wait_until(lambda: jobs.get(job_id).status.is_terminal)
            assert jobs.get(job_id) is not None
    assert not hook.called


def test_callback_url_internal_host_accepted(tmp_path: Path) -> None:
    """http://n8n:5678/... (A§10.2) is accepted by the schema and actually called (no SSRF filter)."""
    with respx.mock(assert_all_mocked=True) as mock:
        hook = mock.post(CALLBACK).mock(return_value=httpx.Response(200))
        with make_client(tmp_path, mock) as client:
            resp = client.post("/scrape", json=BODY)
            assert resp.status_code == 202
            wait_until(lambda: hook.called)
    assert hook.calls[0].request.url.host == "n8n" and hook.calls[0].request.url.port == 5678


def _slow_client(tmp_path: Path, mock: respx.MockRouter) -> TestClient:
    """Like make_client, but the company website hangs so the job stays running."""
    import asyncio

    mock.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": ELEMENTS}))
    mock.get(url__regex=r"https://firma-eins-example\.de/robots\.txt").mock(return_value=httpx.Response(404))

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, headers=HTML, text="<html/>")

    mock.get(url__regex=r"https://firma-eins-example\.de/.*").mock(side_effect=hang)
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "OVERPASS_URL": OVERPASS,
                                    "CRAWLER_PER_DOMAIN_DELAY_S": "0"}))

    async def public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    app.state.pipeline_deps.dns_resolve = public
    app.state.pipeline_deps.gate = OverpassGate(per_minute=600_000)
    return TestClient(app)


def test_max_runtime_timeout_calls_back_once_and_counts_failed(tmp_path: Path, monkeypatch) -> None:
    from leadscraper.jobs import cleanup
    from leadscraper.observability import metrics

    before = metrics.REGISTRY.get_sample_value("scrape_jobs_total", {"status": "failed"}) or 0.0
    before_cancel = metrics.REGISTRY.get_sample_value("scrape_jobs_total", {"status": "cancelled"}) or 0.0
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        hook = mock.post(CALLBACK).mock(return_value=httpx.Response(200))
        with _slow_client(tmp_path, mock) as client:
            jobs = client.app.state.jobs
            job_id = client.post("/scrape", json=BODY).json()["job_id"]
            wait_until(lambda: jobs.get(job_id).progress.get("candidates", 0) > 0)
            monkeypatch.setattr(cleanup.C, "JOB_MAX_RUNTIME_MINUTES", 0.0)   # force the Q5 guard
            client.portal.call(cleanup.sweep_once, jobs, client.app.state.settings)
            wait_until(lambda: hook.called and jobs.get(job_id) is None)
            assert jobs.is_tombstoned(job_id)                                # 200 → deleted
            assert client.get(f"/scrape/{job_id}").status_code == 404
            time.sleep(0.1)
    assert hook.call_count == 1
    sent = json.loads(hook.calls[0].request.content)
    assert sent["status"] == "failed" and sent["job_id"] == job_id
    assert sent["error"]["code"] == "job_timeout"
    after = metrics.REGISTRY.get_sample_value("scrape_jobs_total", {"status": "failed"})
    assert after == before + 1
    assert (metrics.REGISTRY.get_sample_value("scrape_jobs_total", {"status": "cancelled"}) or 0.0) \
        == before_cancel


def test_timeout_callback_500_keeps_job_until_failed_ttl(tmp_path: Path, monkeypatch) -> None:
    from leadscraper.jobs import cleanup

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        hook = mock.post(CALLBACK).mock(return_value=httpx.Response(500))
        with _slow_client(tmp_path, mock) as client:
            jobs = client.app.state.jobs
            job_id = client.post("/scrape", json=BODY).json()["job_id"]
            wait_until(lambda: jobs.get(job_id).progress.get("candidates", 0) > 0)
            monkeypatch.setattr(cleanup.C, "JOB_MAX_RUNTIME_MINUTES", 0.0)
            client.portal.call(cleanup.sweep_once, jobs, client.app.state.settings)
            wait_until(lambda: hook.called)
            body = client.get(f"/scrape/{job_id}").json()
            assert body["status"] == "failed" and body["error"]["code"] == "job_timeout"
    assert hook.call_count == 1


def test_delete_of_running_job_does_not_call_back(tmp_path: Path) -> None:
    from leadscraper.observability import metrics

    before = metrics.REGISTRY.get_sample_value("scrape_jobs_total", {"status": "cancelled"}) or 0.0
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        hook = mock.post(CALLBACK).mock(return_value=httpx.Response(200))
        with _slow_client(tmp_path, mock) as client:
            jobs = client.app.state.jobs
            job_id = client.post("/scrape", json=BODY).json()["job_id"]
            wait_until(lambda: jobs.get(job_id).progress.get("candidates", 0) > 0)
            assert client.delete(f"/scrape/{job_id}").status_code == 204
            time.sleep(0.3)
    assert not hook.called
    assert metrics.REGISTRY.get_sample_value("scrape_jobs_total", {"status": "cancelled"}) == before + 1
