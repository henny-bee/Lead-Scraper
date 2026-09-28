"""Environment settings — exactly the variables of ARCHITECTURE.md §9, with the §9 defaults.

No ``pydantic-settings`` (PLAN.md Q16): a Pydantic v2 model is populated from ``os.environ``.
Only the names in :data:`ENV_VARS` are ever read. Non-env limits/tunables live in
:mod:`leadscraper.constants`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    """All A§9 variables. Field alias = environment variable name."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    app_env: str = Field(default="prod", alias="APP_ENV")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT", ge=1, le=65535)

    # Job lifecycle
    job_ttl_minutes: float = Field(default=15, alias="JOB_TTL_MINUTES", gt=0)
    job_failed_ttl_minutes: float = Field(default=30, alias="JOB_FAILED_TTL_MINUTES", gt=0)
    temp_dir: str = Field(default="/tmp/leadscraper", alias="TEMP_DIR")

    # Resolver
    nominatim_url: str = Field(default="", alias="NOMINATIM_URL")
    nominatim_max_rps: float = Field(default=1, alias="NOMINATIM_MAX_RPS", gt=0)
    industry_embeddings_enabled: bool = Field(default=False, alias="INDUSTRY_EMBEDDINGS_ENABLED")
    industry_llm_enabled: bool = Field(default=False, alias="INDUSTRY_LLM_ENABLED")

    # Sources
    overpass_url: str = Field(default="https://overpass-api.de/api/interpreter", alias="OVERPASS_URL")
    google_places_api_key: str = Field(default="", alias="GOOGLE_PLACES_API_KEY")
    google_places_enabled: bool = Field(default=False, alias="GOOGLE_PLACES_ENABLED")
    companies_house_api_key: str = Field(default="", alias="COMPANIES_HOUSE_API_KEY")
    companies_house_enabled: bool = Field(default=False, alias="COMPANIES_HOUSE_ENABLED")

    # Crawler
    crawler_user_agent: str = Field(
        default="LeadScraperBot/0.3 (+https://your-domain.de/bot)", alias="CRAWLER_USER_AGENT"
    )
    crawler_global_concurrency: int = Field(default=16, alias="CRAWLER_GLOBAL_CONCURRENCY", ge=1)
    crawler_per_domain_delay_s: float = Field(default=2, alias="CRAWLER_PER_DOMAIN_DELAY_S", ge=0)
    crawler_max_pages_per_domain: int = Field(default=5, alias="CRAWLER_MAX_PAGES_PER_DOMAIN", ge=1)
    crawler_max_response_mb: float = Field(default=2, alias="CRAWLER_MAX_RESPONSE_MB", gt=0)

    # Verification
    smtp_verify_enabled: bool = Field(default=False, alias="SMTP_VERIFY_ENABLED")
    smtp_helo_host: str = Field(default="", alias="SMTP_HELO_HOST")
    smtp_mail_from: str = Field(default="", alias="SMTP_MAIL_FROM")

    # Optional security
    api_key: str = Field(default="", alias="API_KEY")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def crawler_max_response_bytes(self) -> int:
        return int(self.crawler_max_response_mb * 1024 * 1024)


#: The exact environment contract (A§9), in declaration order.
ENV_VARS: tuple[str, ...] = tuple(f.alias for f in Settings.model_fields.values() if f.alias)


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]  # tolerate quoted values (A§9 quotes CRAWLER_USER_AGENT)
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``environ`` (default ``os.environ``); reads only :data:`ENV_VARS`.

    An empty value means "use the A§9 default" (e.g. ``PORT=`` behaves like an unset ``PORT``).
    """
    env = os.environ if environ is None else environ
    values: dict[str, str] = {}
    for name in ENV_VARS:
        raw = env.get(name)
        if raw is None:
            continue
        cleaned = _clean(raw)
        if cleaned != "":
            values[name] = cleaned
    return Settings.model_validate(values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings (cached). Tests call ``get_settings.cache_clear()`` after patching env."""
    return load_settings()
