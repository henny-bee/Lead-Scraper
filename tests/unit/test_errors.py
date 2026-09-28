import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from leadscraper.api.errors import ApiError
from leadscraper.main import create_app
from leadscraper.schemas.scrape import ScrapeRequest
from leadscraper.settings import load_settings


@pytest.fixture
def client() -> TestClient:
    app = create_app(load_settings({}))

    @app.post("/_t/scrape")
    async def echo(req: ScrapeRequest) -> dict:
        return {"ok": True}

    @app.get("/_t/api/{status}")
    async def api_error(status: int) -> dict:
        raise ApiError(status, f"code_{status}", f"message {status}", {"k": "v"})

    @app.get("/_t/http/{status}")
    async def http_error(status: int) -> dict:
        raise HTTPException(status_code=status)

    @app.get("/_t/boom")
    async def boom() -> dict:
        raise RuntimeError("secret internals")

    return TestClient(app, raise_server_exceptions=False)


def assert_envelope(resp, status: int, code: str | None = None) -> dict:
    assert resp.status_code == status
    body = resp.json()
    assert body["status"] == "error"
    assert set(body) == {"status", "error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert isinstance(body["error"]["details"], dict)
    if code:
        assert body["error"]["code"] == code
    return body


def test_health_not_ready_before_startup(client: TestClient) -> None:
    """Without the lifespan warm-up the resolver index is not ready → 503 envelope (T24)."""
    body = assert_envelope(client.get("/health"), 503, "not_ready")
    assert body["error"]["details"]["checks"]["resolver_index"] is False


def test_validation_error_envelope(client: TestClient) -> None:
    body = assert_envelope(client.post("/_t/scrape", json={"country": "DE"}), 422, "validation_error")
    locs = [e["loc"] for e in body["error"]["details"]["errors"]]
    assert ["body", "industries"] in locs and ["body", "information"] in locs


def test_invalid_json_envelope(client: TestClient) -> None:
    resp = client.post("/_t/scrape", content=b"{not json", headers={"content-type": "application/json"})
    assert_envelope(resp, 422, "validation_error")


def test_unknown_route_404(client: TestClient) -> None:
    assert_envelope(client.get("/does-not-exist"), 404, "not_found")


def test_method_not_allowed(client: TestClient) -> None:
    assert_envelope(client.delete("/health"), 405, "method_not_allowed")


@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (403, "forbidden"),
                                         (404, "not_found"), (429, "rate_limited")])
def test_http_exceptions(client: TestClient, status: int, code: str) -> None:
    assert_envelope(client.get(f"/_t/http/{status}"), status, code)


def test_api_error(client: TestClient) -> None:
    body = assert_envelope(client.get("/_t/api/422"), 422, "code_422")
    assert body["error"] == {"code": "code_422", "message": "message 422", "details": {"k": "v"}}


def test_unexpected_error_hides_internals(client: TestClient) -> None:
    body = assert_envelope(client.get("/_t/boom"), 500, "internal_error")
    assert "secret" not in body["error"]["message"]
