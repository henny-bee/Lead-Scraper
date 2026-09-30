import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families
from pydantic import ValidationError

from leadscraper import constants
from leadscraper.observability import metrics
from leadscraper.observability.logging import configure_logging, get_logger
from leadscraper.settings import ENV_VARS, load_settings

ROOT = Path(__file__).resolve().parents[2]

A9_ENV = (
    "APP_ENV HOST PORT JOB_TTL_MINUTES JOB_FAILED_TTL_MINUTES TEMP_DIR NOMINATIM_URL "
    "NOMINATIM_MAX_RPS INDUSTRY_EMBEDDINGS_ENABLED INDUSTRY_LLM_ENABLED OVERPASS_URL "
    "GOOGLE_PLACES_API_KEY GOOGLE_PLACES_ENABLED COMPANIES_HOUSE_API_KEY COMPANIES_HOUSE_ENABLED "
    "CRAWLER_USER_AGENT CRAWLER_GLOBAL_CONCURRENCY CRAWLER_PER_DOMAIN_DELAY_S "
    "CRAWLER_MAX_PAGES_PER_DOMAIN CRAWLER_MAX_RESPONSE_MB SMTP_VERIFY_ENABLED SMTP_HELO_HOST "
    "SMTP_MAIL_FROM API_KEY"
).split()
#: /N2: the env contract is the block plus these additions; ``.env.example`` carries them after the
#: values.
V0_4_ENV_ADDITIONS = ["WEB_SEARCH_URL"]
V0_4_ENV_DEFAULTS = {"WEB_SEARCH_URL": "https://html.duckduckgo.com/html/"}


def _dotenv_pairs(text: str) -> dict[str, str]:
    pairs = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


def _architecture_dotenv() -> dict[str, str]:
    arch = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    block = re.search(r"``` dotenv\n(.*?)```", arch, re.S)
    assert block, "A§9 dotenv block not found"
    return _dotenv_pairs(block.group(1))


class SpyEnv(Mapping[str, str]):
    """Records every key the settings loader looks up."""

    def __init__(self, data: dict[str, str]) -> None:
        self.data, self.read = data, set()

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        return self.data[key]

    def get(self, key, default=None):  # type: ignore[override]
        self.read.add(key)
        return self.data.get(key, default)

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("settings must not iterate the whole environment")

    def __len__(self) -> int:
        return len(self.data)


def test_env_contract_matches_a9_exactly() -> None:
    assert list(ENV_VARS) == A9_ENV + V0_4_ENV_ADDITIONS
    assert set(_architecture_dotenv()) == set(A9_ENV)


def test_env_example_equals_a9_block() -> None:
    example = _dotenv_pairs((ROOT / ".env.example").read_text(encoding="utf-8"))
    assert example == {**_architecture_dotenv(), **V0_4_ENV_DEFAULTS}
    assert "DATABASE_URL" not in example and "REDIS_URL" not in example


def test_only_a9_names_are_read() -> None:
    spy = SpyEnv({"DATABASE_URL": "postgres://x", "REDIS_URL": "redis://x", "PORT": "9000"})
    s = load_settings(spy)
    assert spy.read == set(A9_ENV + V0_4_ENV_ADDITIONS)
    assert s.port == 9000


def test_defaults_with_empty_environment() -> None:
    s = load_settings({})
    assert s.app_env == "prod" and s.host == "0.0.0.0" and s.port == 8000
    assert s.job_ttl_minutes == 15 and s.job_failed_ttl_minutes == 30
    assert s.temp_dir == "/tmp/leadscraper"
    assert s.nominatim_url == "" and s.nominatim_max_rps == 1
    assert s.industry_embeddings_enabled is False and s.industry_llm_enabled is False
    assert s.overpass_url == "https://overpass-api.de/api/interpreter"
    assert s.google_places_api_key == "" and s.google_places_enabled is False
    assert s.companies_house_api_key == "" and s.companies_house_enabled is False
    assert s.crawler_user_agent == "LeadScraperBot/0.3 (+https://your-domain.de/bot)"
    assert (s.crawler_global_concurrency, s.crawler_per_domain_delay_s,
            s.crawler_max_pages_per_domain, s.crawler_max_response_mb) == (16, 2, 5, 2)
    assert s.crawler_max_response_bytes == 2 * 1024 * 1024
    assert s.smtp_verify_enabled is False and s.smtp_helo_host == "" and s.smtp_mail_from == ""
    assert s.api_key == "" and s.auth_enabled is False
    assert s.web_search_url == "https://html.duckduckgo.com/html/"


def test_defaults_equal_env_example_values() -> None:
    """Loading.env.example verbatim yields the same settings as an empty environment."""
    example = _dotenv_pairs((ROOT / ".env.example").read_text(encoding="utf-8"))
    assert load_settings(example) == load_settings({})


def test_overrides_are_parsed() -> None:
    s = load_settings({
        "SMTP_VERIFY_ENABLED": "true", "PORT": "9001", "API_KEY": "secret",
        "CRAWLER_USER_AGENT": "'Bot/1.0'", "JOB_TTL_MINUTES": " ",
    })
    assert s.smtp_verify_enabled is True and s.port == 9001 and s.auth_enabled
    assert s.crawler_user_agent == "Bot/1.0"
    assert s.job_ttl_minutes == 15  # empty value -> default


def test_invalid_value_rejected() -> None:
    with pytest.raises(ValidationError):
        load_settings({"PORT": "not-a-port"})


def test_constants_module_holds_limits() -> None:
    assert constants.MAX_OUTPUT_UPPER == 5000
    assert constants.VERIFY_SYNC_LIMIT == 50
    assert constants.VERIFY_BATCH_LIMIT == 10_000
    assert constants.WAIT_MAX_SECONDS == 20
    assert constants.JOB_MAX_RUNTIME_MINUTES == 120
    assert constants.SMTP_GREYLIST_BACKOFF_MINUTES == (5, 15, 60)
    assert constants.CALLBACK_TIMEOUT_S > 0
    assert constants.OVERPASS_MAX_CONCURRENCY == 2          # per endpoint
    assert (constants.CONFIG_DIR / "i18n" / "contact_pages.yaml").is_file()


def test_settings_module_does_not_use_pydantic_settings() -> None:
    src = (ROOT / "src" / "leadscraper" / "settings.py").read_text(encoding="utf-8")
    assert "pydantic_settings" not in src


def test_metrics_exact_names_and_labels() -> None:
    metrics.SCRAPE_JOBS.labels(status="success").inc(0)
    metrics.RESOLVER_RESULTS.labels(field="country", method="exact").inc(0)
    metrics.CANDIDATES_DISCOVERED.labels(country="DE", source="osm").inc(0)
    metrics.CRAWL_REQUESTS.labels(status_code="200").inc(0)
    metrics.CRAWL_DURATION.observe(0.1)
    metrics.EMAIL_FOUND_RATIO.labels(country="DE", region="Bayern", industry="Logistik").set(0.5)
    metrics.TILES_SATURATED.labels(source="osm").inc(0)
    metrics.SOURCE_BUDGET_USED.labels(source="osm").set(0)
    metrics.SMTP_RESULTS.labels(result="unknown").inc(0)
    metrics.WEBSITES_RESOLVED.labels(method="osm_tag").inc(0)
    metrics.CRAWL_JS_SHELLS.inc(0)
    payload, content_type = metrics.render_latest()
    assert content_type.startswith("text/plain")
    samples = {s.name: set(s.labels) for fam in text_string_to_metric_families(payload.decode())
               for s in fam.samples}
    expected_labels = {
        "scrape_jobs_total": {"status"},
        "resolver_results_total": {"field", "method"},
        "candidates_discovered_total": {"country", "source"},
        "crawl_requests_total": {"status_code"},
        "crawl_duration_seconds_count": set(),
        "email_found_ratio": {"country", "region", "industry"},
        "tiles_saturated_total": {"source"},
        "source_budget_used": {"source"},
        "smtp_results_total": {"result"},
        "websites_resolved_total": {"method"},
        "crawl_js_shells_total": set(),
    }
    for name, labels in expected_labels.items():
        assert name in samples, name
        assert samples[name] == labels, name
    assert set(metrics.METRIC_NAMES) <= {n.removesuffix("_count") for n in samples} | {
        "crawl_duration_seconds"}


def test_logging_configures(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("prod")
    get_logger("t").info("hello", job_id="scr_x")
    out = capsys.readouterr().out
    assert '"event": "hello"' in out and '"job_id": "scr_x"' in out
