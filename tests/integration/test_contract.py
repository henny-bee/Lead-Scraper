"""Contract snapshots (A§11 "Contract"): response *shapes* of /scrape (202, running, final),
/scrape/resolve and /verify, compared with committed snapshots in tests/fixtures/contracts/.

A shape keeps every key (in order) and replaces values by their JSON type, so data may change
while the contract (A§2.1–§2.4) may not. Update a snapshot only when the spec changes.
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import dns.name
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from leadscraper.api.deps import RateLimiter
from leadscraper.main import create_app
from leadscraper.services.verify_service import EmailVerifier
from leadscraper.settings import load_settings
from leadscraper.sources.osm_overpass import OverpassGate
from leadscraper.verification import lists
from leadscraper.verification.dns import DnsChecker

SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"
OVERPASS = "https://overpass.test/api/interpreter"
HTML = {"content-type": "text/html; charset=utf-8"}
UNITED_STATES = {"country": "United States", "regions": ["California", "Texas"],
                 "industries": ["Manufacturing", "Logistics"],
                 "information": ["company_name", "company_email", "website"], "max_output": 500}
GERMANY = {"country": "Germany", "regions": ["NRW"], "industries": ["Maschinenbau"],
           "information": ["company_name", "company_email", "website"], "max_output": 10}


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if value is None:
        return "null"
    return {bool: "bool", int: "number", float: "number", str: "string"}[type(value)]


def assert_snapshot(name: str, body) -> None:
    expected = json.loads((SNAPSHOTS / f"{name}.json").read_text(encoding="utf-8"))
    actual = shape(body)
    assert json.dumps(actual) == json.dumps(expected), json.dumps(actual, indent=1)


class MX:
    def __init__(self, host: str) -> None:
        self.preference, self.exchange = 10, dns.name.from_text(host)


class FakeResolver:
    async def resolve(self, qname, rdtype, **_kw):
        return [MX(f"mx01.{qname}.")]


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/":
        return httpx.Response(200, headers=HTML, text=f"<html><p>Firma GmbH</p><p>info@{request.url.host}</p></html>")
    return httpx.Response(404, headers=HTML, text="x")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(OVERPASS).mock(return_value=httpx.Response(200, json={"elements": [
            {"type": "node", "id": 1, "lat": 51.2, "lon": 6.8,
             "tags": {"name": "Firma Maschinenbau", "website": "https://firma-mb-example.de"}}]}))
        mock.get(url__regex=r"https://firma-mb-example\.de/.*").mock(side_effect=site)
        app = create_app(load_settings({"TEMP_DIR": str(tmp_path), "OVERPASS_URL": OVERPASS,
                                        "CRAWLER_PER_DOMAIN_DELAY_S": "0"}))

        async def public(_host: str) -> list[str]:
            return ["93.184.216.34"]

        app.state.pipeline_deps.dns_resolve = public
        app.state.pipeline_deps.gate = OverpassGate(per_minute=600_000)
        app.state.rate_limiter = RateLimiter(per_minute=1e6, burst=10**6)
        app.state.verifier_factory = lambda s: EmailVerifier(
            s, dns=DnsChecker(FakeResolver()), suppression=lists.SuppressionList(path=tmp_path / "none"))
        with TestClient(app) as c:
            yield c


def test_scrape_resolve_contract(client: TestClient) -> None:
    body = client.post("/scrape/resolve", json=UNITED_STATES).json()
    assert_snapshot("scrape_resolve", body)


def test_scrape_lifecycle_contracts(client: TestClient) -> None:
    jobs = client.app.state.jobs
    queued_job, _ = jobs.create("scrape", {**GERMANY, "max_output": 11}, target=11)
    assert_snapshot("scrape_running", client.get(f"/scrape/{queued_job.job_id}").json())
    created = client.post("/scrape", json=GERMANY)
    assert created.status_code == 202
    assert_snapshot("scrape_queued", created.json())
    job_id = created.json()["job_id"]
    for _ in range(300):
        if jobs.get(job_id).status.is_terminal:
            break
        time.sleep(0.02)
    final = client.get(f"/scrape/{job_id}").json()
    assert final["status"] == "success" and final["count"] == 1
    assert_snapshot("scrape_final", final)


def test_verify_contract(client: TestClient) -> None:
    body = client.post("/verify", json={"emails": ["info@example.de"]}).json()
    assert_snapshot("verify_sync", body)


def test_error_contract(client: TestClient) -> None:
    body = client.post("/scrape/resolve", json={**GERMANY, "regions": ["Bayerm"]}).json()
    assert body == {"status": "error", "error": {
        "code": "unresolved_region", "message": "Region 'Bayerm' could not be resolved for country DE",
        "details": {"input": "Bayerm", "suggestions": [{"id": "iso:DE-BY", "name": "Bayern"}]}}}
