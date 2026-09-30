"""/health readiness and /metrics exposition."""

from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from leadscraper.main import create_app
from leadscraper.settings import load_settings

A11_FAMILIES = {"scrape_jobs", "resolver_results", "candidates_discovered", "crawl_requests",
                "crawl_duration_seconds", "email_found_ratio", "tiles_saturated", "source_budget_used",
                "smtp_results"}


def test_health_ready_after_startup_no_network(tmp_path: Path) -> None:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "t")}))
    with respx.mock(assert_all_mocked=True) as mock, TestClient(app) as c:
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "checks": {"resolver_index": True,
                                                          "temp_dir_writable": True}}
        assert list((tmp_path / "t").iterdir()) == []                  # probe file removed
    assert mock.calls.call_count == 0


def test_health_503_when_temp_dir_not_writable(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "t")}))
    with TestClient(app) as c:
        app.state.jobs.temp_root = blocker / "sub"                      # parent is a file
        resp = c.get("/health")
        app.state.jobs.temp_root = tmp_path / "t"
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == "not_ready" and err["details"]["reasons"] == ["temp_dir_writable"]


def test_health_503_when_resolver_not_warm(tmp_path: Path) -> None:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path)}))
    with TestClient(app) as c:
        app.state.resolver_ready = False
        resp = c.get("/health")
    assert resp.status_code == 503 and "resolver_index" in resp.json()["error"]["message"]


def test_metrics_exposes_the_nine_a11_families(tmp_path: Path) -> None:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path)}))
    with TestClient(app) as c:
        c.post("/scrape/resolve", json={"country": "Germany", "industries": ["Logistik"],
                                        "information": ["website"]})
        resp = c.get("/metrics")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/plain")
    families = {f.name for f in text_string_to_metric_families(resp.text)}
    assert A11_FAMILIES <= families
    assert 'resolver_results_total{field="country",method="exact"}' in resp.text
    for name in ("scrape_jobs_total", "resolver_results_total", "candidates_discovered_total",
                 "crawl_requests_total", "crawl_duration_seconds", "email_found_ratio",
                 "tiles_saturated_total", "source_budget_used", "smtp_results_total"):
        assert f"# TYPE {name.removesuffix('_total')}" in resp.text, name


@pytest.mark.parametrize("path", ["/metrics"])
def test_metrics_requires_key_when_configured_but_health_does_not(tmp_path: Path, path: str) -> None:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path), "API_KEY": "k"}))
    with TestClient(app) as c:
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-API-Key": "k"}).status_code == 200
        assert c.get("/health").status_code == 200
