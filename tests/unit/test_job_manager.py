import asyncio
import re
from pathlib import Path

import pytest

from leadscraper.domain.models import JobStatus
from leadscraper.jobs.manager import JOB_ID_RE, JobManager, JobState, new_job_id

pytestmark = pytest.mark.anyio

BODY = {"country": "Germany", "regions": ["NRW"], "industries": ["Logistik"],
        "information": ["company_email"], "max_output": 10}


def test_job_id_format() -> None:
    a, b = new_job_id("scrape"), new_job_id("verify")
    assert re.fullmatch(r"scr_[0-9A-HJKMNP-TV-Z]{26}", a)
    assert re.fullmatch(r"vrf_[0-9A-HJKMNP-TV-Z]{26}", b)
    assert JOB_ID_RE.match(a) and JOB_ID_RE.match(b)
    # time-ordered prefix (ULID-like)
    assert new_job_id("scrape", 1000.0)[4:14] < new_job_id("scrape", 2000.0)[4:14]
    assert len({new_job_id("scrape", 1.0) for _ in range(1000)}) == 1000


async def test_create_returns_queued_and_temp_dir(tmp_path: Path) -> None:
    jm = JobManager(tmp_path)
    job, created = jm.create("scrape", BODY, target=10)
    assert created and job.status is JobStatus.QUEUED and job.job_id.startswith("scr_")
    assert job.temp_dir == tmp_path / job.job_id
    assert job.crawl_dir.is_dir()
    assert job.candidates_path == job.temp_dir / "candidates.jsonl"
    assert job.result_path == job.temp_dir / "result.json"
    assert job.progress == {"target": 10, "candidates": 0, "crawled": 0, "with_email": 0}


async def test_background_runner_updates_state(tmp_path: Path) -> None:
    jm = JobManager(tmp_path)
    job, _ = jm.create("scrape", BODY, target=10)
    seen: list[JobStatus] = []

    async def runner(j: JobState) -> None:
        seen.append(j.status)
        jm.update_progress(j.job_id, candidates=5, crawled=3, with_email=2, count=2)
        j.candidates_path.write_text("{}\n")
        jm.set_result(j.job_id, [{"company_email": "info@firma-example.de"}], count=1)

    await jm.start(job, runner)
    assert seen == [JobStatus.RUNNING]
    assert job.status is JobStatus.SUCCESS and job.finished_at is not None
    assert job.progress["candidates"] == 5 and job.count == 1
    assert job.result == [{"company_email": "info@firma-example.de"}]


async def test_runner_exception_marks_failed(tmp_path: Path) -> None:
    jm = JobManager(tmp_path)
    job, _ = jm.create("scrape", BODY)

    async def runner(j: JobState) -> None:
        raise RuntimeError("boom")

    await jm.start(job, runner)
    assert job.status is JobStatus.FAILED
    assert job.error == {"code": "internal_error", "message": "Job failed unexpectedly",
                         "details": {"error": "RuntimeError"}}


async def test_idempotent_while_in_ram(tmp_path: Path) -> None:
    jm = JobManager(tmp_path)
    job1, c1 = jm.create("scrape", BODY)
    job2, c2 = jm.create("scrape", dict(reversed(list(BODY.items()))))  # key order irrelevant
    assert c1 and not c2 and job1 is job2
    other, c3 = jm.create("scrape", {**BODY, "max_output": 11})
    assert c3 and other.job_id != job1.job_id
    verify, c4 = jm.create("verify", BODY)       # different kind -> different job
    assert c4 and verify.job_id.startswith("vrf_")
    await jm.delete(job1.job_id)
    job3, c5 = jm.create("scrape", BODY)
    assert c5 and job3.job_id != job1.job_id


async def test_failed_job_not_reused(tmp_path: Path) -> None:
    jm = JobManager(tmp_path)
    job, _ = jm.create("scrape", BODY)
    jm.fail(job.job_id, "x", "y")
    again, created = jm.create("scrape", BODY)
    assert created and again.job_id != job.job_id


async def test_delete_cancels_running_task_and_removes_temp_dir(tmp_path: Path) -> None:
    jm = JobManager(tmp_path)
    job, _ = jm.create("scrape", BODY)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def runner(j: JobState) -> None:
        j.candidates_path.write_text('{"name": "x"}\n')
        (j.crawl_dir / "page.html").write_text("<html></html>")
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = jm.start(job, runner)
    await started.wait()
    assert job.status is JobStatus.RUNNING and job.temp_dir.is_dir()
    assert await jm.delete(job.job_id) is True
    assert cancelled.is_set() and task.done()
    assert jm.get(job.job_id) is None and job.job_id not in jm.jobs
    assert not job.temp_dir.exists()
    assert list(tmp_path.iterdir()) == []
    assert await jm.delete(job.job_id) is False            # unknown now


async def test_delete_with_tombstone_holds_no_data(tmp_path: Path) -> None:
    jm = JobManager(tmp_path, clock=lambda: 1000.0)
    job, _ = jm.create("scrape", BODY)
    jm.set_result(job.job_id, [{"company_email": "info@firma-example.de"}], count=1)
    await jm.delete(job.job_id, tombstone=True)
    assert jm.is_tombstoned(job.job_id)
    assert jm.tombstones == {job.job_id: 1000.0}
    assert job.result is None


async def test_delete_from_inside_own_task(tmp_path: Path) -> None:
    """e.g. callback 2xx deletes the job from within the job's task — must not deadlock."""
    jm = JobManager(tmp_path)
    job, _ = jm.create("scrape", BODY)

    async def runner(j: JobState) -> None:
        await jm.delete(j.job_id, tombstone=True)

    await asyncio.wait_for(jm.start(job, runner), 5)
    assert jm.get(job.job_id) is None and not job.temp_dir.exists()


async def test_purge_stale_temp_only_removes_job_dirs(tmp_path: Path) -> None:
    stale = tmp_path / new_job_id("scrape")
    (stale / "crawl").mkdir(parents=True)
    (stale / "result.json").write_text("{}")
    stale_vrf = tmp_path / new_job_id("verify")
    stale_vrf.mkdir()
    unrelated = tmp_path / "keep-me.txt"
    unrelated.write_text("operator file")
    jm = JobManager(tmp_path)
    assert jm.purge_stale_temp() == 2
    assert not stale.exists() and not stale_vrf.exists() and unrelated.exists()


async def test_purge_creates_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "a" / "b"
    JobManager(root).purge_stale_temp()
    assert root.is_dir()


async def test_close_deletes_everything(tmp_path: Path) -> None:
    jm = JobManager(tmp_path)
    job, _ = jm.create("scrape", BODY)
    started = asyncio.Event()

    async def runner(j: JobState) -> None:
        started.set()
        await asyncio.sleep(3600)

    jm.start(job, runner)
    done, _ = jm.create("scrape", {**BODY, "max_output": 2})
    await started.wait()
    await jm.close()
    assert jm.jobs == {} and jm.tombstones == {} and list(tmp_path.iterdir()) == []


def test_app_startup_purges_and_shutdown_cleans(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from leadscraper.main import create_app
    from leadscraper.settings import load_settings

    stale = tmp_path / new_job_id("scrape")
    stale.mkdir()
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path)}))
    with TestClient(app) as client:
        assert not stale.exists()
        job, _ = app.state.jobs.create("scrape", BODY)
        assert job.temp_dir.is_dir()
        assert client.get("/health").status_code == 200
    assert app.state.jobs.jobs == {} and list(tmp_path.iterdir()) == []
