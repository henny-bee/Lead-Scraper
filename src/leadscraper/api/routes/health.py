"""``GET /health`` (liveness + readiness) and ``GET /metrics`` (ARCHITECTURE.md §2.5 table, §6, §11).

``/health`` is ready (``200``) only when the resolver's CLDR country index has been warmed at
startup and ``TEMP_DIR`` is writable; otherwise ``503`` with the failing checks (uniform error
envelope, code ``not_ready``). It makes no network call and is never authenticated or rate-limited
(Q6), so container healthchecks can use it. ``/metrics`` serves the Prometheus exposition of the
dedicated registry (the nine A§11 metric families); it requires the API key when one is set.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response

from leadscraper.api.errors import error_response
from leadscraper.observability.metrics import render_latest

router = APIRouter(tags=["health"])
metrics_router = APIRouter(tags=["health"])


def temp_dir_writable(root: Path) -> bool:
    probe = root / f".health-{secrets.token_hex(4)}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


@router.get("/health")
async def health(request: Request) -> Any:
    checks = {
        "resolver_index": bool(getattr(request.app.state, "resolver_ready", False)),
        "temp_dir_writable": temp_dir_writable(Path(request.app.state.jobs.temp_root)),
    }
    if all(checks.values()):
        return {"status": "ok", "checks": checks}
    reasons = [name for name, ok in checks.items() if not ok]
    return error_response(503, "not_ready", f"Service not ready: {', '.join(reasons)}",
                          {"checks": checks, "reasons": reasons})


@metrics_router.get("/metrics")
async def metrics() -> Response:
    payload, content_type = render_latest()
    return Response(content=payload, media_type=content_type)
