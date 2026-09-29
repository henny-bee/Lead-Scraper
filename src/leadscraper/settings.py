"""Environment settings — the variables of, with the defaults, plus ``WEB_SEARCH_URL``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: the one env var added to the contract (default = DuckDuckGo HTML).
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"         # == constants.DUCKDUCKGO_HTML_URL
_SEARCH_OFF = "off"                                           # == constants.WEB_SEARCH_OFF


class Settings(BaseModel):
    """All variables."""

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

    # additions, after the block.: web search for website lookup + discovery.
    web_search_url: str = Field(default=_DDG_HTML_URL, alias="WEB_SEARCH_URL")

    @field_validator("web_search_url")
    @classmethod
    def _check_web_search_url(cls, value: str) -> str:
        v = value.strip()
        if v.lower() == _SEARCH_OFF:
            return _SEARCH_OFF
        try:
            parts = urlsplit(v)
        except ValueError as exc:
            raise ValueError("WEB_SEARCH_URL must be 'off' or an http(s) URL") from exc
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            raise ValueError("WEB_SEARCH_URL must be 'off' or an http(s) URL")
        labels = host.split(".")
        if "google" in labels[:-1] or host == "bing.com" or host.endswith(".bing.com"):
            raise ValueError("Google/Bing are not supported (robots.txt disallows /search)")
        return v

    @property
    def web_search_enabled(self) -> bool:
        return self.web_search_url != _SEARCH_OFF

    @property
    def web_search_backend(self) -> str | None:
        """``"duckduckgo"`` | ``"searxng"`` | ``None`` (off)."""
        if not self.web_search_enabled:
            return None
        return "duckduckgo" if self.web_search_url == _DDG_HTML_URL else "searxng"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def crawler_max_response_bytes(self) -> int:
        return int(self.crawler_max_response_mb * 1024 * 1024)


#: The environment contract: in declaration order, then the additions.
ENV_VARS: tuple[str, ...] = tuple(f.alias for f in Settings.model_fields.values() if f.alias)


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]  # tolerate quoted values
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``environ`` (default ``os.environ``); reads only:data:`ENV_VARS`."""
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
    """Process-wide settings (cached)."""
    return load_settings()
