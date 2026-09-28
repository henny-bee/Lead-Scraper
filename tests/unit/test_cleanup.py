import asyncio
import dataclasses
from pathlib import Path

import pytest

from leadscraper import constants as C
from leadscraper.domain.models import JobStatus
from leadscraper.jobs.cleanup import (
    CleanupSweeper,
    delete_after_delivery,
    on_callback_success,
    sweep_once,
)
from leadscraper.jobs.manager import JobManager, JobState
from leadscraper.settings import load_settings

pytestmark = pytest.mark.anyio

SETTINGS = load_settings({})          # JOB_TTL_MINUTES=15, JOB_FAILED_TTL_MINUTES=30
MIN = 60.0


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += minutes * MIN


def body(n: int) -> dict:
    return {"country": "DE", "industries": ["Logistik"], "information": ["website"], "max_output": n}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def jm(tmp_path: Path, clock: Clock) -> JobManager:
    return JobManager(tmp_path, clock=clock)


async def finished_job(jm: JobManager, n: int, status: JobStatus) -> JobState:
    job, _ = jm.create("scrape", body(n))
    jm.set_result(job.job_id, [{"company_email": "info@firma-example.de"}], count=1)
    job.candidates_path.write_text("{}\n")
    if status is JobStatus.FAILED:
        jm.fail(job.job_id, "internal_error", "x")
    else:
        jm.set_status(job.job_id, status)
    return job


async def test_success_job_deleted_after_ttl(jm: JobManager, clock: Clock) -> None:
    job = await finished_job(jm, 1, JobStatus.SUCCESS)
    clock.advance(14.9)
    assert (await sweep_once(jm, SETTINGS)).expired_success == 0
    assert jm.get(job.job_id) is not None and job.temp_dir.exists()
    clock.advance(0.2)
    assert (await sweep_once(jm, SETTINGS)).expired_success == 1
    assert jm.get(job.job_id) is None and not job.temp_dir.exists()
    assert not jm.is_tombstoned(job.job_id)          # TTL deletion leaves no tombstone


async def test_failed_job_survives_15_deleted_after_30(jm: JobManager, clock: Clock) -> None:
    job = await finished_job(jm, 2, JobStatus.FAILED)
    clock.advance(15.5)
    await sweep_once(jm, SETTINGS)
    assert jm.get(job.job_id) is not None and job.error["code"] == "internal_error"
    clock.advance(14.6)
    assert (await sweep_once(jm, SETTINGS)).expired_failed == 1
    assert jm.get(job.job_id) is None and not job.temp_dir.exists()


async def test_cancelled_job_removed_promptly(jm: JobManager) -> None:
    job = await finished_job(jm, 3, JobStatus.CANCELLED)
    assert (await sweep_once(jm, SETTINGS)).removed_cancelled == 1
    assert jm.get(job.job_id) is None and not job.temp_dir.exists()


async def test_running_job_never_ttl_deleted(jm: JobManager, clock: Clock) -> None:
    job, _ = jm.create("scrape", body(4))
    jm.set_status(job.job_id, JobStatus.RUNNING)
    clock.advance(C.JOB_MAX_RUNTIME_MINUTES - 1)       # way past both TTLs
    await sweep_once(jm, SETTINGS)
    assert jm.get(job.job_id) is not None and job.status is JobStatus.RUNNING


async def test_runtime_guard_times_out_then_failed_ttl(jm: JobManager, clock: Clock) -> None:
    job, _ = jm.create("scrape", body(5))
    started = asyncio.Event()

    async def runner(j: JobState) -> None:
        started.set()
        await asyncio.sleep(3600)

    task = jm.start(job, runner)
    await started.wait()
    clock.advance(C.JOB_MAX_RUNTIME_MINUTES)
    report = await sweep_once(jm, SETTINGS)
    assert report.timed_out == 1 and task.done()
    assert job.status is JobStatus.FAILED
    assert job.error["code"] == "job_timeout"
    assert job.temp_dir.exists()                        # kept for failed-TTL so client can read error
    clock.advance(SETTINGS.job_failed_ttl_minutes - 1)
    await sweep_once(jm, SETTINGS)
    assert jm.get(job.job_id) is not None
    clock.advance(1)
    assert (await sweep_once(jm, SETTINGS)).expired_failed == 1
    assert jm.get(job.job_id) is None and not job.temp_dir.exists()


async def test_queued_job_without_start_also_guarded(jm: JobManager, clock: Clock) -> None:
    job, _ = jm.create("scrape", body(6))
    clock.advance(C.JOB_MAX_RUNTIME_MINUTES)
    assert (await sweep_once(jm, SETTINGS)).timed_out == 1
    assert job.status is JobStatus.FAILED and job.error["code"] == "job_timeout"


async def test_delivery_deletion_leaves_data_free_tombstone(jm: JobManager, clock: Clock) -> None:
    job = await finished_job(jm, 7, JobStatus.SUCCESS)
    assert await delete_after_delivery(jm, job.job_id) is True
    assert jm.get(job.job_id) is None and not job.temp_dir.exists()
    # tombstone = id -> timestamp only
    assert jm.tombstones == {job.job_id: clock.t}
    assert all(isinstance(v, float) for v in jm.tombstones.values())
    assert job.result is None and job.resolved is None
    clock.advance(SETTINGS.job_ttl_minutes - 0.1)
    await sweep_once(jm, SETTINGS)
    assert jm.is_tombstoned(job.job_id)
    clock.advance(0.2)
    assert (await sweep_once(jm, SETTINGS)).expired_tombstones == 1
    assert not jm.is_tombstoned(job.job_id) and jm.tombstones == {}


async def test_callback_success_hook_deletes_immediately(jm: JobManager) -> None:
    job = await finished_job(jm, 8, JobStatus.SUCCESS)
    assert await on_callback_success(jm, job.job_id)
    assert jm.get(job.job_id) is None and jm.is_tombstoned(job.job_id)
    assert await delete_after_delivery(jm, "scr_unknown") is False


async def test_custom_ttls(tmp_path: Path, clock: Clock) -> None:
    settings = load_settings({"JOB_TTL_MINUTES": "1", "JOB_FAILED_TTL_MINUTES": "2"})
    jm = JobManager(tmp_path, clock=clock)
    ok = await finished_job(jm, 9, JobStatus.SUCCESS)
    bad = await finished_job(jm, 10, JobStatus.FAILED)
    clock.advance(1)
    await sweep_once(jm, settings)
    assert jm.get(ok.job_id) is None and jm.get(bad.job_id) is not None
    clock.advance(1)
    await sweep_once(jm, settings)
    assert jm.get(bad.job_id) is None


async def test_sweeper_loop_runs_and_stops(jm: JobManager, clock: Clock) -> None:
    job = await finished_job(jm, 11, JobStatus.CANCELLED)
    sweeper = CleanupSweeper(jm, SETTINGS, interval_s=0.01)
    sweeper.start()
    assert sweeper.running
    for _ in range(200):
        if jm.get(job.job_id) is None:
            break
        await asyncio.sleep(0.01)
    assert jm.get(job.job_id) is None
    await sweeper.stop()
    assert not sweeper.running
    await sweeper.stop()                                # idempotent


async def test_sweeper_survives_errors(jm: JobManager, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def broken(*_a):
        nonlocal calls
        calls += 1
        raise RuntimeError("x")

    monkeypatch.setattr("leadscraper.jobs.cleanup.sweep_once", broken)
    sweeper = CleanupSweeper(jm, SETTINGS, interval_s=0.01)
    sweeper.start()
    for _ in range(200):
        if calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert calls >= 2 and sweeper.running
    await sweeper.stop()


def test_lifespan_starts_and_stops_sweeper(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from leadscraper.main import create_app

    app = create_app(load_settings({"TEMP_DIR": str(tmp_path)}))
    with TestClient(app):
        assert app.state.sweeper.running
        app.state.jobs.create("scrape", body(12))
        assert any(tmp_path.iterdir())
    assert not app.state.sweeper.running
    assert list(tmp_path.iterdir()) == []                # shutdown deletes all temp dirs


def test_report_is_plain_counts() -> None:
    from leadscraper.jobs.cleanup import SweepReport

    assert dataclasses.asdict(SweepReport()) == {
        "expired_success": 0, "expired_failed": 0, "removed_cancelled": 0,
        "timed_out": 0, "expired_tombstones": 0}


async def test_verify_jobs_follow_same_ttl_rules(jm: JobManager, clock: Clock) -> None:
    ok, _ = jm.create("verify", {"emails": ["a@b-example.de"] * 60})
    jm.set_status(ok.job_id, JobStatus.SUCCESS)
    bad, _ = jm.create("verify", {"emails": ["c@d-example.de"] * 60})
    jm.fail(bad.job_id, "internal_error", "x")
    clock.advance(SETTINGS.job_ttl_minutes)
    await sweep_once(jm, SETTINGS)
    assert jm.get(ok.job_id) is None and jm.get(bad.job_id) is not None
    clock.advance(SETTINGS.job_failed_ttl_minutes)
    await sweep_once(jm, SETTINGS)
    assert jm.get(bad.job_id) is None and not bad.temp_dir.exists()
