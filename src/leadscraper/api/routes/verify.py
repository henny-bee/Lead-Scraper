"""``POST /verify`` and ``GET /verify/{job_id}``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from leadscraper import constants as C
from leadscraper.api.errors import not_found
from leadscraper.domain.models import JobStatus
from leadscraper.jobs.manager import JobManager, JobState
from leadscraper.schemas.verify import VerifyRequest, VerifyResult
from leadscraper.services.verify_service import smtp_available
from leadscraper.settings import Settings

router = APIRouter(tags=["verify"])

SMTP_DISABLED_WARNING = ("SMTP verification is not enabled in this deployment (SMTP_VERIFY_ENABLED / "
                         "SMTP_HELO_HOST / SMTP_MAIL_FROM); results are at most DNS level (unknown).")


def _dump(results: list[VerifyResult]) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json", exclude_none=True) for r in results]


def _warnings(settings: Settings, req: VerifyRequest) -> list[str]:
    return [SMTP_DISABLED_WARNING] if req.smtp_check and not smtp_available(settings) else []


def _final_body(results: list[dict[str, Any]], warnings: list[str],
                job_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"status": "success"}
    if job_id:
        body["job_id"] = job_id
    body.update({"count": len(results), "results": results})
    if warnings:
        body["warnings"] = warnings
    return body


@router.post("/verify")
async def verify(req: VerifyRequest, request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    warnings = _warnings(settings, req)
    if len(req.emails) <= C.VERIFY_SYNC_LIMIT:
        verifier = request.app.state.verifier_factory(settings)   # per-request caches only
        results = await verifier.verify_many(req.emails, smtp_check=req.smtp_check,
                                             retry_greylist=False)
        return JSONResponse(_final_body(_dump(results), warnings))

    jobs: JobManager = request.app.state.jobs
    job, created = jobs.create("verify", req.model_dump(mode="json"), target=len(req.emails))
    if created:
        job.warnings = warnings

        async def runner(j: JobState) -> None:
            verifier = request.app.state.verifier_factory(settings)   # per-job caches only
            done: list[dict[str, Any]] = []
            for i, email in enumerate(req.emails, 1):
                result = await verifier.verify(email, smtp_check=req.smtp_check, retry_greylist=True)
                done.append(result.model_dump(mode="json", exclude_none=True))
                jobs.update_progress(j.job_id, checked=i, count=i)
            jobs.set_result(j.job_id, done, count=len(done))

        jobs.start(job, runner)
    return JSONResponse(status_code=202, content={"status": "queued" if created else job.status.value,
                                                  "job_id": job.job_id,
                                                  "poll_url": f"/verify/{job.job_id}"})


@router.get("/verify/{job_id}")
async def get_verify_job(job_id: str, request: Request) -> dict[str, Any]:
    jobs: JobManager = request.app.state.jobs
    job = jobs.get(job_id)
    if job is None or job.kind != "verify":
        raise not_found("Verify job", job_id)
    if job.status is JobStatus.SUCCESS:
        return _final_body(list(job.result or []), list(job.warnings), job.job_id)  # no delete
    if job.status is JobStatus.FAILED:
        return {"status": "failed", "job_id": job.job_id, "error": job.error}
    if job.status is JobStatus.CANCELLED:
        return {"status": "cancelled", "job_id": job.job_id}
    progress = {"target": job.progress.get("target", 0), "checked": job.progress.get("checked", 0)}
    return {"status": job.status.value, "job_id": job.job_id, "count": job.count, "progress": progress}
