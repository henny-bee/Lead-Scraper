"""Final job response bodies shared by ``GET /scrape/{id}`` and the callback."""

from __future__ import annotations

from typing import Any

from leadscraper.jobs.manager import JobState


def final_body(job: JobState, companies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """``{"status":"success","job_id","count","companies"}``."""
    items = list(job.result or []) if companies is None else companies
    return {"status": "success", "job_id": job.job_id, "count": len(job.result or []),
            "companies": items}


def failed_body(job: JobState) -> dict[str, Any]:
    """``{"status":"failed","job_id","error":{code,message,details}}``."""
    return {"status": "failed", "job_id": job.job_id, "error": job.error}
