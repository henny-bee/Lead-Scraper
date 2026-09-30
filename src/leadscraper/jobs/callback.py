"""Callback delivery."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from leadscraper import constants as C
from leadscraper.domain.models import JobStatus
from leadscraper.jobs.bodies import failed_body, final_body
from leadscraper.jobs.cleanup import on_callback_success
from leadscraper.jobs.manager import JobManager, JobState
from leadscraper.observability.logging import get_logger

log = get_logger(__name__)
ClientFactory = Callable[[], httpx.AsyncClient]


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=C.CALLBACK_TIMEOUT_S, follow_redirects=False)


async def deliver_callback(job: JobState, jobs: JobManager, *, user_agent: str,
                           client_factory: ClientFactory = _default_client) -> bool:
    """POST the final body to ``job.callback_url``; returns True when delivered (2xx)."""
    if not job.callback_url or job.status not in (JobStatus.SUCCESS, JobStatus.FAILED):
        return False
    body = final_body(job) if job.status is JobStatus.SUCCESS else failed_body(job)
    try:
        async with client_factory() as client:
            resp = await client.post(job.callback_url, json=body,
                                     headers={"User-Agent": user_agent})
    except httpx.HTTPError as exc:
        log.warning("callback_failed", job_id=job.job_id, error=type(exc).__name__)
        return False
    if 200 <= resp.status_code < 300:
        log.info("callback_delivered", job_id=job.job_id, status_code=resp.status_code)
        await on_callback_success(jobs, job.job_id)
        return True
    log.warning("callback_rejected", job_id=job.job_id, status_code=resp.status_code)
    return False
