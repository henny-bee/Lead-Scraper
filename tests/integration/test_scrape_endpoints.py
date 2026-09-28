"""/scrape endpoints (A§2.1, A§2.3, A§2.5, A§3.3; Q3, Q10, Q25) with mocked Overpass + websites."""

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from leadscraper.main import create_app
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import OverpassGate

OVERPASS = "https://overpass.test/api/interpreter"
HTML = {"content-type": "text/html; charset=utf-8"}
BODY = {"country": "Germany", "regions": ["NRW"], "industries": ["Maschinenbau"],
        "information": ["company_name", "company_email", "website"], "max_output": 10}
ELEMENTS = [
    {"type": "node", "id": i, "lat": 51.2, "lon": 6.8,
     "tags": {"name": f"Firma {i} Maschinenbau", "website": f"https://firma{i}-example.de"}}
    for i in range(1, 4)
]


def site(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if request.url.path == "/":
        return httpx.Response(200, headers=HTML, text='<html><a href="/impressum">Impressum</a></html>')
    if request.url.path == "/impressum":
        n = host.removeprefix("firma").removesuffix(".de")
        return httpx.Response(200, headers=HTML,
                              text=f"<html><p>Firma {n} Maschinenbau GmbH</p><p>info@{host}</p></html>")
    return httpx.Response(404, headers=HTML, text="x")


@pytest.fixture
def web() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": ELEMENTS}))
        mock.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))
        mock.get(url__regex=r"https://firma\d-example\.de/.*").mock(side_effect=site)
        yield mock


@pytest.fixture
def client(tmp_path: Path, web) -> Iterator[TestClient]:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "OVERPASS_URL": OVERPASS,
                                    "CRAWLER_PER_DOMAIN_DELAY_S": "0"}))

    async def public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    app.state.pipeline_deps.dns_resolve = public
    app.state.pipeline_deps.gate = OverpassGate(per_minute=600_000)
    with TestClient(app) as c:
        yield c


def wait_final(client: TestClient, job_id: str) -> None:
    jobs = client.app.state.jobs
    for _ in range(300):
        job = jobs.get(job_id)
        if job is not None and job.status.is_terminal:
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_post_returns_202_queued_contract(client: TestClient) -> None:
    resp = client.post("/scrape", json=BODY)
    assert resp.status_code == 202
    body = resp.json()
    assert list(body) == ["status", "job_id", "poll_url"]
    assert body["status"] == "queued" and body["job_id"].startswith("scr_")
    assert body["poll_url"] == f"/scrape/{body['job_id']}"
    again = client.post("/scrape", json=BODY).json()                    # idempotent while in RAM
    assert again["job_id"] == body["job_id"]


def test_running_body_contract(client: TestClient) -> None:
    job, _ = client.app.state.jobs.create("scrape", BODY, target=10)      # not started → queued
    body = client.get(f"/scrape/{job.job_id}").json()
    assert list(body) == ["status", "job_id", "count", "progress", "resolved"]
    assert body["status"] == "queued" and body["count"] == 0
    assert body["progress"] == {"target": 10, "candidates": 0, "crawled": 0, "with_email": 0}


def test_final_get_contract_and_repeatable_reads(client: TestClient) -> None:
    """T28: the final GET no longer deletes — it can be read again and exported afterwards."""
    job_id = client.post("/scrape", json=BODY).json()["job_id"]
    wait_final(client, job_id)
    temp = Path(client.app.state.jobs.temp_root) / job_id
    final = client.get(f"/scrape/{job_id}").json()
    assert list(final) == ["status", "job_id", "count", "companies"]
    assert final["status"] == "success" and final["count"] == 3
    assert list(final["companies"][0]) == ["company_name", "company_email", "website",
                                           "country", "region", "industry"]
    assert {c["region"] for c in final["companies"]} == {"NRW"}
    again = client.get(f"/scrape/{job_id}")
    assert again.status_code == 200 and again.json() == final
    export = client.get(f"/scrape/{job_id}/export", params={"format": "csv"})
    assert export.status_code == 200 and len(export.text.strip().splitlines()) == 4
    assert client.get(f"/scrape/{job_id}/export").status_code == 200
    assert temp.exists() and client.app.state.jobs.get(job_id) is not None
    assert client.delete(f"/scrape/{job_id}").status_code == 204          # explicit delete still works
    assert not temp.exists() and client.get(f"/scrape/{job_id}").status_code == 404
    assert client.delete(f"/scrape/{job_id}").status_code == 204          # tombstone
    assert client.post("/scrape", json=BODY).json()["job_id"] != job_id   # new job after deletion


def test_pagination_never_deletes(client: TestClient) -> None:
    job_id = client.post("/scrape", json=BODY).json()["job_id"]
    wait_final(client, job_id)
    page1 = client.get(f"/scrape/{job_id}", params={"offset": 0, "limit": 2}).json()
    assert page1["count"] == 3 and len(page1["companies"]) == 2
    assert page1["offset"] == 0 and page1["limit"] == 2
    page2 = client.get(f"/scrape/{job_id}", params={"offset": 2, "limit": 2}).json()
    assert len(page2["companies"]) == 1
    assert client.app.state.jobs.get(job_id) is not None                  # last page: kept (T28)
    assert client.get(f"/scrape/{job_id}").status_code == 200


def test_wait_returns_final_body(client: TestClient) -> None:
    resp = client.post("/scrape", params={"wait": 20}, json=BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success" and body["count"] == 3 and len(body["companies"]) == 3
    assert client.get(f"/scrape/{body['job_id']}").status_code == 200     # ?wait no longer deletes


def test_new_job_does_not_delete_finished_jobs(client: TestClient) -> None:
    """T28 (user request 2026-09-25): creating another job leaves earlier results readable."""
    jobs = client.app.state.jobs
    old_id = client.post("/scrape", json=BODY).json()["job_id"]
    wait_final(client, old_id)
    new_id = client.post("/scrape", json={**BODY, "max_output": 11}).json()["job_id"]
    assert new_id != old_id and jobs.get(old_id) is not None
    assert client.get(f"/scrape/{old_id}").status_code == 200
    assert client.get(f"/scrape/{old_id}/export").status_code == 200


def test_wait_above_limit_rejected(client: TestClient) -> None:
    resp = client.post("/scrape", params={"wait": 21}, json=BODY)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error"


def test_unresolvable_input_422_before_job(client: TestClient) -> None:
    resp = client.post("/scrape", json={**BODY, "regions": ["Bayerm"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unresolved_region"
    assert client.app.state.jobs.jobs == {}


def test_csv_export_and_xlsx(client: TestClient) -> None:
    job_id = client.post("/scrape", json=BODY).json()["job_id"]
    wait_final(client, job_id)
    x = client.get(f"/scrape/{job_id}/export", params={"format": "xlsx"})
    assert x.status_code == 422 and x.json()["error"]["code"] == "unsupported_format"
    resp = client.get(f"/scrape/{job_id}/export", params={"format": "csv"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0] == "company_name,company_email,website,country,region,industry"
    assert len(lines) == 4 and all(line.endswith("Germany,NRW,Maschinenbau") for line in lines[1:])
    assert client.get(f"/scrape/{job_id}").status_code == 200             # export never deletes (T28)


def test_export_of_running_job_conflict(client: TestClient) -> None:
    job, _ = client.app.state.jobs.create("scrape", BODY)
    resp = client.get(f"/scrape/{job.job_id}/export")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "job_not_finished"


def test_delete_running_job_204_and_temp_dir_gone(client: TestClient) -> None:
    jobs = client.app.state.jobs
    job, _ = jobs.create("scrape", BODY)

    async def slow(j) -> None:
        await asyncio.sleep(3600)

    client.portal.call(lambda: _start(jobs, job, slow))
    assert job.temp_dir.exists()
    assert client.delete(f"/scrape/{job.job_id}").status_code == 204
    assert not job.temp_dir.exists() and jobs.get(job.job_id) is None
    assert client.delete(f"/scrape/{job.job_id}").status_code == 204        # tombstoned
    assert client.get(f"/scrape/{job.job_id}").status_code == 404


async def _start(jobs, job, runner) -> None:
    jobs.start(job, runner)


def test_failed_job_body(client: TestClient) -> None:
    jobs = client.app.state.jobs
    job, _ = jobs.create("scrape", BODY)
    jobs.fail(job.job_id, "internal_error", "Job failed unexpectedly", {"error": "RuntimeError"})
    assert client.get(f"/scrape/{job.job_id}").json() == {
        "status": "failed", "job_id": job.job_id,
        "error": {"code": "internal_error", "message": "Job failed unexpectedly",
                  "details": {"error": "RuntimeError"}}}
    assert jobs.get(job.job_id) is not None                               # failed-TTL applies


def test_unknown_ids_404(client: TestClient) -> None:
    for resp in (client.get("/scrape/scr_00000000000000000000000000"),
                 client.delete("/scrape/scr_00000000000000000000000000"),
                 client.get("/scrape/scr_00000000000000000000000000/export")):
        assert resp.status_code == 404 and resp.json()["status"] == "error"
    verify_job, _ = client.app.state.jobs.create("verify", {"emails": ["a@b-example.de"] * 51})
    assert client.get(f"/scrape/{verify_job.job_id}").status_code == 404
