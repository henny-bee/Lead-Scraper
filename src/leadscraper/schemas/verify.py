"""``/verify`` schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from leadscraper import constants as C


class VerifyResultValue(StrEnum):
    DELIVERABLE = "deliverable"
    UNDELIVERABLE = "undeliverable"
    RISKY = "risky"
    UNKNOWN = "unknown"
    SUPPRESSED = "suppressed"


class VerificationLevel(StrEnum):
    SYNTAX = "syntax"
    DNS = "dns"
    SMTP = "smtp"


class VerifyRequest(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=C.VERIFY_BATCH_LIMIT)
    smtp_check: bool = False


class VerifyChecks(BaseModel):
    """Only checks that actually ran are emitted."""

    syntax_valid: bool | None = None
    domain_has_mx: bool | None = None
    mx_hosts: list[str] | None = None
    is_disposable: bool | None = None
    is_role_account: bool | None = None
    is_free_provider: bool | None = None
    is_catch_all: bool | None = None
    smtp_code: int | None = None


class VerifyResult(BaseModel):
    email: str
    result: VerifyResultValue
    reason: str
    score: float = Field(ge=0, le=1)
    verification_level: VerificationLevel
    checks: VerifyChecks
    cached: bool = False              # always false: no cross-request cache
    checked_at: str                   # ISO-8601 UTC, e.g. "2026-09-23T09:14:02Z"


class VerifyResponse(BaseModel):
    status: str = "success"
    count: int
    results: list[VerifyResult]
