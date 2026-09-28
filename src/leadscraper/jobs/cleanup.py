"""TTL cleanup, runtime guard and callback-2xx deletion helper (A§2.5 "Cleanup rules", A§8, A§9).

Rules applied by :func:`sweep_once` (all based on the manager's injectable clock):
- ``success`` jobs (read or not): deleted ``JOB_TTL_MINUTES`` after they finished;
- ``failed`` jobs: deleted ``JOB_FAILED_TTL_MINUTES`` after they failed;
- ``cancelled`` jobs: deleted at the next sweep;
- ``queued``/``running`` jobs are never TTL-deleted, but a job running longer than
  ``constants.JOB_MAX_RUNTIME_MINUTES`` is cancelled and marked ``failed``/``job_timeout`` (Q5),
  after which the failed-TTL applies;
- tombstones (data-free, Q3) expire after ``JOB_TTL_MINUTES`` (``constants.TOMBSTONE_TTL_MINUTES``).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from leadscraper import constants as C
from leadscraper.domain.models import JobStatus
from leadscraper.jobs.manager import JobManager
from leadscraper.observability.logging import get_logger
from leadscraper.settings import Settings

log = get_logger(__name__)


@dataclass(slots=True)
class SweepReport:
    expired_success: int = 0
    expired_failed: int = 0
    removed_cancelled: int = 0
    timed_out: int = 0
    expired_tombstones: int = 0


def tombstone_ttl_s(settings: Settings) -> float:
    minutes = C.TOMBSTONE_TTL_MINUTES
    return (settings.job_ttl_minutes if minutes is None else minutes) * 60


async def sweep_once(jobs: JobManager, settings: Settings) -> SweepReport:
    report = SweepReport()
    now = jobs.clock()
    success_ttl = settings.job_ttl_minutes * 60
    failed_ttl = settings.job_failed_ttl_minutes * 60
    max_runtime = C.JOB_MAX_RUNTIME_MINUTES * 60

    for job in list(jobs.jobs.values()):
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            since = job.started_at if job.started_at is not None else job.created_at
            if now - since >= max_runtime:
                jobs.fail(job.job_id, C.JOB_TIMEOUT_ERROR_CODE,
                          f"Job exceeded the maximum runtime of {C.JOB_MAX_RUNTIME_MINUTES} minutes",
                          {"max_runtime_minutes": C.JOB_MAX_RUNTIME_MINUTES})
                await jobs.cancel_task(job)
                report.timed_out += 1
                log.warning("job_timeout", job_id=job.job_id)
            continue
        finished = job.finished_at if job.finished_at is not None else job.created_at
        if job.status is JobStatus.SUCCESS and now - finished >= success_ttl:
            await jobs.delete(job.job_id)
            report.expired_success += 1
        elif job.status is JobStatus.FAILED and now - finished >= failed_ttl:
            await jobs.delete(job.job_id)
            report.expired_failed += 1
        elif job.status is JobStatus.CANCELLED:
            await jobs.delete(job.job_id)
            report.removed_cancelled += 1

    ttl = tombstone_ttl_s(settings)
    for job_id, deleted_at in list(jobs.tombstones.items()):
        if now - deleted_at >= ttl:
            del jobs.tombstones[job_id]
            report.expired_tombstones += 1
    return report


async def delete_after_delivery(jobs: JobManager, job_id: str) -> bool:
    """Callback 2xx (A§2.5): delete RAM state + temp dir immediately, leaving only a data-free
    tombstone. Since T28 (user request) reading a result via GET/export no longer deletes it."""
    return await jobs.delete(job_id, tombstone=True)


#: Callback ``2xx`` (A§2.5) uses the same deletion path.
on_callback_success = delete_after_delivery


class CleanupSweeper:
    """Background loop started in the app lifespan; stops cleanly on shutdown."""

    def __init__(self, jobs: JobManager, settings: Settings,
                 interval_s: float = C.CLEANUP_SWEEP_INTERVAL_S) -> None:
        self.jobs, self.settings, self.interval_s = jobs, settings, interval_s
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="cleanup-sweeper")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await sweep_once(self.jobs, self.settings)
            except Exception:  # never let the sweeper die
                log.exception("cleanup_sweep_failed")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
