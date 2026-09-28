"""Scrape request/response schemas (ARCHITECTURE.md §2.1, §2.3, §2.5 code block).

The ``ScrapeRequest`` model follows the A§2.5 code block. Changes, all intended:
- optional ``callback_url`` (PLAN.md Q1);
- numeric bounds come from :mod:`leadscraper.constants` (Q19) instead of literals (same values);
- bug fix: ``industries`` is re-checked *after* strip/dedupe, because Pydantic applies
  ``min_length`` before the after-validator (``["  "]`` would otherwise become ``[]``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, ValidationInfo, field_validator

from leadscraper import constants as C


class InformationField(StrEnum):
    COMPANY_NAME = "company_name"
    COMPANY_EMAIL = "company_email"
    WEBSITE = "website"
    PHONE = "phone"
    ADDRESS = "address"
    LEGAL_FORM = "legal_form"
    REGISTER_NUMBER = "register_number"
    VAT_ID = "vat_id"


class ScrapeRequest(BaseModel):
    country: str = Field(min_length=2, max_length=100, examples=["Germany", "France"])
    regions: list[str] = Field(default_factory=list, max_length=C.MAX_REGIONS)  # empty = whole country
    industries: list[str] = Field(min_length=C.MIN_INDUSTRIES, max_length=C.MAX_INDUSTRIES)
    information: list[InformationField] = Field(min_length=1)
    max_output: int = Field(default=C.MAX_OUTPUT_DEFAULT, ge=1, le=C.MAX_OUTPUT_UPPER)
    # --- optional, additions from the original spec ---
    verify_emails: bool = False
    freshness_days: int = Field(default=C.FRESHNESS_DAYS_DEFAULT, ge=1, le=C.FRESHNESS_DAYS_MAX)
    exclude_marketing_objections: bool = True
    # --- PLAN.md Q1: optional webhook (A§2.5, A§8, A§10.2) ---
    callback_url: HttpUrl | None = None

    @field_validator("regions", "industries")
    @classmethod
    def strip_and_dedupe(cls, values: list[str], info: ValidationInfo) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for v in (s.strip() for s in values):
            if v and v.casefold() not in seen:
                seen.add(v.casefold())
                out.append(v)
        if info.field_name == "industries" and len(out) < C.MIN_INDUSTRIES:
            raise ValueError("industries must contain at least one non-empty value")
        return out


class QueuedResponse(BaseModel):
    """``202`` body of ``POST /scrape`` (A§2.1)."""

    status: str = "queued"
    job_id: str
    poll_url: str


class Progress(BaseModel):
    """``progress`` block of a running job (A§2.3)."""

    target: int
    candidates: int = 0
    crawled: int = 0
    with_email: int = 0


class RunningResponse(BaseModel):
    """``GET /scrape/{job_id}`` while queued/running (A§2.3); ``resolved`` per A§2.2."""

    status: str
    job_id: str
    count: int
    progress: Progress
    resolved: dict[str, Any] | None = None


class FinalResponse(BaseModel):
    """``GET /scrape/{job_id}`` when finished (A§2.3). Company keys depend on ``information``."""

    status: str
    job_id: str
    count: int
    companies: list[dict[str, Any]]
