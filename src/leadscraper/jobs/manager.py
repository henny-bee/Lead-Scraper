"""In-memory job manager (ARCHITECTURE.md §2.5, §3.1, §8): ``jobs: dict[str, JobState]``.

- Job ids: ``scr_``/``vrf_`` + 26-char Crockford base32 (48-bit ms timestamp + 80 random bits),
  stdlib only (PLAN.md Q20).
- Per-job temp dir ``TEMP_DIR/{job_id}/`` with ``crawl/``; ``candidates.jsonl`` and ``result.json``
  are written there by the pipeline (A§8).
- Idempotency (A§2.1): hash of the normalised body → same ``job_id`` while the job is in RAM.
  Failed/cancelled jobs are not reused, so a client can retry (A§1 "for retry/status").
- Delete = cancel the task if running + remove the dict entry + remove the temp dir. Deletion by
  ``DELETE`` or callback 2xx leaves a data-free tombstone (``job_id`` → deleted_at only;
  PLAN.md Q3/Q25). Reading a result never deletes it (PLAN.md T28).
- Single event loop, no locks needed: every mutation below is synchronous except ``delete``,
  which only awaits the cancelled task before touching the filesystem.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from leadscraper import constants as C
from leadscraper.domain.models import JobStatus
from leadscraper.observability.logging import get_logger

log = get_logger(__name__)

JobKind = Literal["scrape", "verify"]
Clock = Callable[[], float]
Runner = Callable[["JobState"], Awaitable[None]]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PREFIX = {"scrape": C.SCRAPE_JOB_PREFIX, "verify": C.VERIFY_JOB_PREFIX}
JOB_ID_RE = re.compile(rf"^(?:{C.SCRAPE_JOB_PREFIX}|{C.VERIFY_JOB_PREFIX})[{_CROCKFORD}]{{26}}$")


def new_job_id(kind: JobKind, now: float | None = None) -> str:
    """``scr_`` + ULID-like id: 10 chars of millisecond timestamp + 16 chars of randomness."""
    ms = int((time.time() if now is None else now) * 1000) & ((1 << 48) - 1)
    value = (ms << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return _PREFIX[kind] + "".join(reversed(chars))


def request_hash(kind: JobKind, body: dict[str, Any]) -> str:
    """Stable hash of a normalised (already validated/stripped/deduped) request body."""
    canonical = json.dumps({"kind": kind, "body": body}, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class JobState:
    job_id: str
    kind: JobKind
    request: dict[str, Any]
    request_hash: str
    temp_dir: Path
    created_at: float
    status: JobStatus = JobStatus.QUEUED
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float | None = None
    progress: dict[str, int] = field(default_factory=dict)
    count: int = 0
    resolved: dict[str, Any] | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    callback_url: str | None = None
    task: asyncio.Task[None] | None = None

    @property
    def candidates_path(self) -> Path:
        return self.temp_dir / C.CANDIDATES_FILE

    @property
    def crawl_dir(self) -> Path:
        return self.temp_dir / C.CRAWL_DIR

    @property
    def result_path(self) -> Path:
        return self.temp_dir / C.RESULT_FILE


class JobManager:
    def __init__(self, temp_root: str | Path, clock: Clock = time.time) -> None:
        self.temp_root = Path(temp_root)
        self.clock = clock
        self.jobs: dict[str, JobState] = {}
        self.tombstones: dict[str, float] = {}      # job_id -> deleted_at (no job data)
        self._by_hash: dict[str, str] = {}

    # --- startup ---------------------------------------------------------------------------------
    def purge_stale_temp(self) -> int:
        """Remove leftover job dirs from a previous process (A§8: restart = jobs lost).

        Only entries that look like job ids are removed, so a mis-set ``TEMP_DIR`` (e.g. ``/tmp``)
        cannot wipe unrelated files.
        """
        self.temp_root.mkdir(parents=True, exist_ok=True)
        removed = 0
        for entry in self.temp_root.iterdir():
            if JOB_ID_RE.match(entry.name) and entry.name not in self.jobs:
                _rmtree(entry)
                removed += 1
        return removed

    # --- creation --------------------------------------------------------------------------------
    def create(self, kind: JobKind, body: dict[str, Any], *,
               callback_url: str | None = None, target: int = 0) -> tuple[JobState, bool]:
        """Return ``(job, created)``; ``created=False`` when an identical job is still in RAM."""
        digest = request_hash(kind, body)
        existing_id = self._by_hash.get(digest)
        if existing_id is not None:
            existing = self.jobs.get(existing_id)
            if existing is not None and existing.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                return existing, False
        now = self.clock()
        job_id = new_job_id(kind, now)
        while job_id in self.jobs or job_id in self.tombstones:
            job_id = new_job_id(kind, now)
        temp_dir = self.temp_root / job_id
        (temp_dir / C.CRAWL_DIR).mkdir(parents=True, exist_ok=True)
        job = JobState(job_id=job_id, kind=kind, request=body, request_hash=digest,
                       temp_dir=temp_dir, created_at=now, updated_at=now, callback_url=callback_url,
                       progress={"target": target, "candidates": 0, "crawled": 0, "with_email": 0})
        self.jobs[job_id] = job
        self._by_hash[digest] = job_id
        log.info("job_created", job_id=job_id, kind=kind)
        return job, True

    def start(self, job: JobState, runner: Runner) -> asyncio.Task[None]:
        """Run ``runner(job)`` as a background asyncio task in this process (A§2.5, C5)."""
        job.task = asyncio.create_task(self._run(job, runner), name=f"job:{job.job_id}")
        return job.task

    async def _run(self, job: JobState, runner: Runner) -> None:
        self.set_status(job.job_id, JobStatus.RUNNING)
        try:
            await runner(job)
        except asyncio.CancelledError:
            if job.job_id in self.jobs and not job.status.is_terminal:
                self.set_status(job.job_id, JobStatus.CANCELLED)
            raise
        except Exception as exc:  # unexpected pipeline error -> failed with error envelope
            log.exception("job_failed", job_id=job.job_id)
            self.fail(job.job_id, "internal_error", "Job failed unexpectedly",
                      {"error": type(exc).__name__})
        else:
            if job.job_id in self.jobs and not job.status.is_terminal:
                self.set_status(job.job_id, JobStatus.SUCCESS)

    # --- access / updates ------------------------------------------------------------------------
    def get(self, job_id: str) -> JobState | None:
        return self.jobs.get(job_id)

    def is_tombstoned(self, job_id: str) -> bool:
        return job_id in self.tombstones

    def set_status(self, job_id: str, status: JobStatus) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        now = self.clock()
        job.status = status
        job.updated_at = now
        if status is JobStatus.RUNNING and job.started_at is None:
            job.started_at = now
        if status.is_terminal:
            job.finished_at = now

    def update_progress(self, job_id: str, *, count: int | None = None, **counters: int) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.progress.update(counters)
        if count is not None:
            job.count = count
        job.updated_at = self.clock()

    def set_result(self, job_id: str, result: Any, *, count: int | None = None) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.result = result
        if count is not None:
            job.count = count
        job.updated_at = self.clock()

    def fail(self, job_id: str, code: str, message: str,
             details: dict[str, Any] | None = None) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.error = {"code": code, "message": message, "details": details or {}}
        self.set_status(job_id, JobStatus.FAILED)

    # --- deletion --------------------------------------------------------------------------------
    async def cancel_task(self, job: JobState) -> None:
        """Cancel a running task and wait until it has stopped (never awaits itself)."""
        task = job.task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def delete(self, job_id: str, *, tombstone: bool = False) -> bool:
        """Cancel if running, drop RAM state and temp dir. Returns False for unknown ids."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        await self.cancel_task(job)
        self._forget(job, tombstone=tombstone)
        return True

    def _forget(self, job: JobState, *, tombstone: bool) -> None:
        self.jobs.pop(job.job_id, None)
        if self._by_hash.get(job.request_hash) == job.job_id:
            del self._by_hash[job.request_hash]
        if job.task is not None and not job.task.done() and job.task is not asyncio.current_task():
            job.task.cancel()
        job.result = job.resolved = job.error = None     # drop references to job data
        job.task = None
        _rmtree(job.temp_dir)
        if tombstone:
            self.tombstones[job.job_id] = self.clock()
        log.info("job_deleted", job_id=job.job_id, tombstone=tombstone)

    async def close(self) -> None:
        """Shutdown: cancel every task, delete every job and its temp dir."""
        for job_id in list(self.jobs):
            await self.delete(job_id)
        self.tombstones.clear()
        if self.temp_root.is_dir():
            self.purge_stale_temp()


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
