"""Uniform error envelope for every endpoint (ARCHITECTURE.md §2.3).

``{"status": "error", "error": {"code": ..., "message": ..., "details": {...}}}`` for validation
(422), auth (401/403), not found (404), rate limit (429) and unexpected errors (500).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from leadscraper.observability.logging import get_logger
from leadscraper.schemas.common import ErrorBody, ErrorEnvelope
from leadscraper.services.resolver.resolve import ResolutionError

log = get_logger(__name__)

#: Default error codes for plain HTTP errors (FastAPI/Starlette ``HTTPException``).
HTTP_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
}


class ApiError(Exception):
    """Raise from handlers/services to return the uniform envelope."""

    def __init__(self, status_code: int, code: str, message: str,
                 details: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers


def not_found(what: str, ident: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} '{ident}' not found", {"id": ident})


def error_response(status_code: int, code: str, message: str,
                   details: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, details=details or {}))
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body), headers=headers)


async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _resolution_error(_: Request, exc: ResolutionError) -> JSONResponse:
    return error_response(422, exc.code, exc.message, exc.details)


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return error_response(422, "validation_error", "Request validation failed", {"errors": errors})


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = HTTP_STATUS_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ")
    details = exc.detail if isinstance(exc.detail, dict) else {}
    return error_response(exc.status_code, code, message, details, getattr(exc, "headers", None))


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", path=request.url.path, error=type(exc).__name__)
    return error_response(500, "internal_error", "Internal server error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)  # type: ignore[arg-type]
    app.add_exception_handler(ResolutionError, _resolution_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unexpected_error)
