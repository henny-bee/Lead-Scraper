"""Shared schemas: the uniform error envelope."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """``{"status": "error", "error": {"code", "message", "details"}}``."""

    status: Literal["error"] = "error"
    error: ErrorBody
