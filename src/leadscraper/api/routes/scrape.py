"""Scrape endpoints (ARCHITECTURE.md §2.1–§2.3, §2.5, §3.3).

- ``POST /scrape/resolve`` — dry run (A§2.2).
- ``POST /scrape[?wait=N]`` — input is resolved first (ambiguous input → ``422`` with suggestions,
  A§2.1), then an in-memory job is created (idempotent while in RAM) → ``202`` ``{status, job_id,
  poll_url}``. With ``wait`` (≤ ``WAIT_MAX_SECONDS``) the call returns ``200`` with the final body
  if the job finishes in time.
- ``GET /scrape/{job_id}[?offset&limit]`` — running body (+ ``resolved`` block) or the final body
  (A§2.3). A ``failed`` job returns ``{"status":"failed","job_id","error":{…}}``.
- ``GET /scrape/{job_id}/export?format=csv`` — CSV via stdlib; ``xlsx`` → ``422
  unsupported_format`` (Q10).
- ``DELETE /scrape/{job_id}`` — cancel if running + delete → ``204``; ``204`` also for tombstoned
  ids; unknown → ``404`` (Q25).

Retention (PLAN T28, user request 2026-09-25, deviation from Q3): reading a result (final GET,
pages, ``?wait`` 200, CSV export) never deletes it, so it can be read/exported any number of times.
A job is removed by ``DELETE``, callback 2xx, or the TTL sweeper (15 min after finishing, 30 min
after failing).
"""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from leadscraper import constants as C
from leadscraper.api.errors import ApiError, not_found
from leadscraper.domain.models import JobStatus
from leadscraper.jobs.bodies import failed_body, final_body
from leadscraper.jobs.manager import JobManager, JobState
from leadscraper.schemas.scrape import ScrapeRequest
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.services.scrape_service import runner_for

router = APIRouter(tags=["scrape"])
CONTEXT_COLUMNS = ("country", "region", "industry")


def get_resolver(request: Request) -> Resolver:
    return request.app.state.resolver


def _jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def _scrape_job(request: Request, job_id: str) -> JobState:
    job = _jobs(request).get(job_id)
    if job is None or job.kind != "scrape":
        raise not_found("Job", job_id)
    return job


def running_body(job: JobState) -> dict[str, Any]:
    p = job.progress
    return {"status": job.status.value, "job_id": job.job_id, "count": job.count,
            "progress": {"target": p.get("target", 0), "candidates": p.get("candidates", 0),
                         "crawled": p.get("crawled", 0), "with_email": p.get("with_email", 0)},
            "resolved": job.resolved}


@router.post("/scrape/resolve")
async def resolve_scrape(body: ScrapeRequest, request: Request) -> dict[str, Any]:
    """Dry run: how the free-form input is interpreted; no scraping (A§2.2)."""
    resolved = await get_resolver(request).resolve(body)
    return {"status": "success", "resolved": resolved.to_block(known_in_job=0)}


@router.post("/scrape")
async def create_scrape(body: ScrapeRequest, request: Request,
                        wait: int = Query(default=0, ge=0, le=C.WAIT_MAX_SECONDS)) -> JSONResponse:
    await get_resolver(request).resolve(body)          # 422 with suggestions before any job exists
    jobs = _jobs(request)
    payload = body.model_dump(mode="json")
    job, created = jobs.create("scrape", payload, target=body.max_output,
                               callback_url=str(body.callback_url) if body.callback_url else None)
    if created:
        jobs.start(job, runner_for(request.app.state.pipeline_deps, jobs))
    if wait and job.task is not None and not job.status.is_terminal:
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=wait)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        except Exception:                               # job failure is reported via its status
            pass
    if job.status is JobStatus.SUCCESS and wait:
        return JSONResponse(final_body(job))            # T28: reading never deletes
    if job.status is JobStatus.FAILED and wait:
        return JSONResponse(failed_body(job))
    return JSONResponse(status_code=202, content={"status": "queued" if created else job.status.value,
                                                  "job_id": job.job_id,
                                                  "poll_url": f"/scrape/{job.job_id}"})


@router.get("/scrape/{job_id}")
async def get_scrape(request: Request, job_id: str,
                     offset: int | None = Query(default=None, ge=0),
                     limit: int | None = Query(default=None, ge=1, le=C.RESULT_PAGE_LIMIT_MAX)
                     ) -> dict[str, Any]:
    job = _scrape_job(request, job_id)
    if job.status is JobStatus.FAILED:
        return failed_body(job)
    if job.status is JobStatus.CANCELLED:
        return {"status": "cancelled", "job_id": job.job_id}
    if job.status is not JobStatus.SUCCESS:
        return running_body(job)
    companies = list(job.result or [])
    if offset is None and limit is None:
        return final_body(job)                                        # T28: reading never deletes
    start = offset or 0
    size = limit or C.RESULT_PAGE_LIMIT_MAX
    return {**final_body(job, companies[start:start + size]), "offset": start, "limit": size}


@router.get("/scrape/{job_id}/export")
async def export_scrape(request: Request, job_id: str,
                        format: str = Query(default="csv")) -> Response:  # noqa: A002
    if format.lower() != "csv":
        raise ApiError(422, "unsupported_format", f"Export format '{format}' is not supported",
                       {"format": format, "supported": ["csv"]})
    job = _scrape_job(request, job_id)
    if job.status is not JobStatus.SUCCESS:
        raise ApiError(409, "job_not_finished", f"Job '{job_id}' has status {job.status.value}",
                       {"status": job.status.value})
    request_info = [str(x) for x in job.request.get("information", [])]
    columns = [*request_info, *CONTEXT_COLUMNS]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in job.result or []:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
    content = buf.getvalue()                                          # T28: export never deletes
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{job_id}.csv"'})


@router.delete("/scrape/{job_id}", status_code=204)
async def delete_scrape(request: Request, job_id: str) -> Response:
    jobs = _jobs(request)
    job = jobs.get(job_id)
    if job is not None and job.kind == "scrape":
        await jobs.delete(job_id, tombstone=True)       # cancel if running + RAM + temp dir
        return Response(status_code=204)
    if jobs.is_tombstoned(job_id):
        return Response(status_code=204)
    raise not_found("Job", job_id)
