"""Prometheus metrics with the exact names/labels of ARCHITECTURE.md §11.

A dedicated registry keeps the exposition limited to the service's own metrics.
Note on naming: ``prometheus_client`` appends ``_total`` to counters, so counters are declared
without the suffix; ``source_budget_used`` has no ``_total`` in A§11 and is therefore a Gauge.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

SCRAPE_JOBS = Counter(
    "scrape_jobs", "Scrape jobs by final status", ["status"], registry=REGISTRY
)  # -> scrape_jobs_total{status}
RESOLVER_RESULTS = Counter(
    "resolver_results", "Resolver outcomes per field and method", ["field", "method"],
    registry=REGISTRY,
)  # -> resolver_results_total{field,method}
CANDIDATES_DISCOVERED = Counter(
    "candidates_discovered", "Candidates discovered per country and source", ["country", "source"],
    registry=REGISTRY,
)  # -> candidates_discovered_total{country,source}
CRAWL_REQUESTS = Counter(
    "crawl_requests", "Crawler HTTP requests by status code", ["status_code"], registry=REGISTRY
)  # -> crawl_requests_total{status_code}
CRAWL_DURATION = Histogram(
    "crawl_duration_seconds", "Crawler request duration in seconds", registry=REGISTRY
)
EMAIL_FOUND_RATIO = Gauge(
    "email_found_ratio", "Share of candidates yielding an email", ["country", "region", "industry"],
    registry=REGISTRY,
)
TILES_SATURATED = Counter(
    "tiles_saturated", "Quadtree cells still full at minimum size", ["source"], registry=REGISTRY
)  # -> tiles_saturated_total{source}
SOURCE_BUDGET_USED = Gauge(
    "source_budget_used", "Requests used against the per-job/source budget", ["source"],
    registry=REGISTRY,
)
SMTP_RESULTS = Counter(
    "smtp_results", "SMTP probe results", ["result"], registry=REGISTRY
)  # -> smtp_results_total{result}

#: Exposed metric family names (as they appear in the exposition), for tests / docs.
METRIC_NAMES: tuple[str, ...] = (
    "scrape_jobs_total",
    "resolver_results_total",
    "candidates_discovered_total",
    "crawl_requests_total",
    "crawl_duration_seconds",
    "email_found_ratio",
    "tiles_saturated_total",
    "source_budget_used",
    "smtp_results_total",
)


def render_latest() -> tuple[bytes, str]:
    """Return (payload, content_type) for ``GET /metrics``."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
