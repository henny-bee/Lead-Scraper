"""GET /contacts and POST /websites/find"""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from leadscraper.extractors.socials import PLATFORMS, canonical, page_socials
from leadscraper.main import create_app
from leadscraper.settings import load_settings

HTML = {"content-type": "text/html; charset=utf-8"}
HOME = """<html lang="de"><head><title>Acme Logistik GmbH – Spedition</title>
<meta name="description" content="Ihre Spedition in Bremen."></head><body>
<a href="/impressum">Impressum</a> <a href="/kontakt">Kontakt</a>
<footer><a href="https://www.linkedin.com/company/acme-logistik/">LinkedIn</a>
<a href="https://twitter.com/intent/tweet?text=x">share</a>
<a href="https://www.facebook.com/AcmeLogistik">fb</a></footer></body></html>"""
IMPRESSUM = """<html><body><h1>Impressum</h1><p>Acme Logistik GmbH</p><p>Hafenstraße 1, 28195 Bremen</p>
<p>Telefon: +49 421 1234567</p><p>E-Mail: info@acme-logistik.de</p>
<p>Datenschutz: datenschutz@acme-logistik.de</p></body></html>"""
KONTAKT = """<html><body><p>Schreiben Sie uns: info@acme-logistik.de oder vertrieb@acme-logistik.de</p>
<p>Partner: kontakt@partner-example.com</p></body></html>"""
PAGES = {"/": HOME, "/impressum": IMPRESSUM, "/kontakt": KONTAKT}


def acme(request: httpx.Request) -> httpx.Response:
    if request.url.path in PAGES:
        return httpx.Response(200, headers=HTML, text=PAGES[request.url.path])
    return httpx.Response(404, headers=HTML, text="<html>404</html>")


@pytest.fixture
def web() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(url__regex=r"https?://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))
        mock.get(url__regex=r"https://(www\.)?acme-logistik\.de/.*").mock(side_effect=acme)
        mock.get(url__regex=r"https?://.*").mock(return_value=httpx.Response(404, headers=HTML, text="x"))
        yield mock


def make_client(tmp_path: Path, resolve) -> TestClient:
    app = create_app(load_settings({"TEMP_DIR": str(tmp_path / "jobs"), "WEB_SEARCH_URL": "off",
                                    "CRAWLER_PER_DOMAIN_DELAY_S": "0"}))
    app.state.pipeline_deps.dns_resolve = resolve
    return TestClient(app)


async def public(_host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def client(tmp_path: Path, web) -> Iterator[TestClient]:
    with make_client(tmp_path, public) as c:
        yield c


# --- GET /contacts ------------------------------------------------------------------------------
def test_contacts_full_shape_and_values(client: TestClient) -> None:
    resp = client.get("/contacts", params={"website": "acme-logistik.de"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body) == ["domain", "title", "description", "emails", "phones", *PLATFORMS, "error"]
    assert body["error"] is None and body["domain"] == "acme-logistik.de"
    assert body["title"] == "Acme Logistik GmbH – Spedition"
    assert body["description"] == "Ihre Spedition in Bremen."
    emails = {e["value"]: e for e in body["emails"]}
    assert body["emails"][0]["value"] == "info@acme-logistik.de"          # best first
    assert emails["info@acme-logistik.de"]["is_likely_official"] is True
    assert set(emails["info@acme-logistik.de"]["sources"]) == {
        "https://acme-logistik.de/impressum", "https://acme-logistik.de/kontakt"}
    assert emails["datenschutz@acme-logistik.de"]["is_likely_official"] is False   # special function
    assert emails["kontakt@partner-example.com"]["is_likely_official"] is False   # other domain
    assert body["phones"] == ["+494211234567"]
    assert body["linkedins"] == [{"value": "https://linkedin.com/company/acme-logistik",
                                  "sources": ["https://acme-logistik.de/"], "is_likely_official": True}]
    assert [f["value"] for f in body["facebooks"]] == ["https://facebook.com/AcmeLogistik"]
    assert body["twitters"] == []                                          # share link dropped


def test_contacts_homepage_mode_fetches_one_page(client: TestClient, web: respx.MockRouter) -> None:
    body = client.get("/contacts", params={"website": "https://acme-logistik.de/",
                                           "mode": "homepage"}).json()
    pages = [c.request.url.path for c in web.calls if c.request.url.path != "/robots.txt"]
    assert pages == ["/"] and body["emails"] == [] and body["title"]


def test_contacts_unreachable_keeps_shape(tmp_path: Path, web) -> None:
    async def nxdomain(_host: str) -> list[str]:
        return []

    with make_client(tmp_path, nxdomain) as c:
        body = c.get("/contacts", params={"website": "acme-logistik.de"}).json()
    assert body["error"].startswith("unreachable") and body["emails"] == [] and body["linkedins"] == []


def test_contacts_invalid_website_and_mode(client: TestClient) -> None:
    body = client.get("/contacts", params={"website": "facebook.com/acme"}).json()
    assert body["error"].startswith("invalid website") and body["emails"] == []
    assert client.get("/contacts", params={"website": "acme.de", "mode": "all"}).status_code == 422
    assert client.get("/contacts").status_code == 422


# --- POST /websites/find ------------------------------------------------------------------------
def test_find_website_by_guess_with_location(client: TestClient) -> None:
    resp = client.post("/websites/find", json={"name": "Acme Logistik GmbH", "country": "DE",
                                                "city": "Bremen", "postcode": "28195", "industry": "x"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"website": "https://acme-logistik.de", "method": "guess"}


def test_find_website_rejects_wrong_location(client: TestClient) -> None:
    body = client.post("/websites/find", json={"name": "Acme Logistik GmbH", "country": "DE",
                                               "city": "München", "postcode": "80331"}).json()
    assert body == {"website": None, "method": None}


def test_find_website_from_email_domain(client: TestClient) -> None:
    body = client.post("/websites/find", json={"name": "Acme Logistik",
                                               "email": "info@acme-logistik.de"}).json()
    assert body == {"website": "https://acme-logistik.de", "method": "email_domain"}


def test_find_website_requires_name(client: TestClient) -> None:
    assert client.post("/websites/find", json={"city": "Bremen"}).status_code == 422


# --- social extraction --------------------------------------------------------------------------
@pytest.mark.parametrize("platform,raw,expected", [
    ("linkedins", "https://www.linkedin.com/company/vercel/", "https://linkedin.com/company/vercel"),
    ("twitters", "https://twitter.com/vercel?ref=x", "https://x.com/vercel"),
    ("twitters", "https://x.com/intent/tweet?text=a", None),
    ("facebooks", "https://www.facebook.com/sharer/sharer.php?u=x", None),
    ("facebooks", "https://facebook.com/profile.php?id=12345", "https://facebook.com/profile.php?id=12345"),
    ("facebooks", "https://facebook.com/wix", None),
    ("youtubes", "https://www.youtube.com/@VercelHQ", "https://youtube.com/@VercelHQ"),
    ("youtubes", "https://www.youtube.com/watch?v=abc", None),
    ("instagrams", "https://instagram.com/p/xyz", None),
    ("whatsapps", "https://api.whatsapp.com/send?phone=4915112345678&text=hi", "https://wa.me/4915112345678"),
    ("tiktoks", "https://www.tiktok.com/@vercel/video/123", "https://tiktok.com/@vercel"),
])
def test_social_canonical(platform: str, raw: str, expected: str | None) -> None:
    assert canonical(platform, raw) == expected


def test_github_only_from_anchors() -> None:
    html = '<a href="https://github.com/acme">gh</a><script>"https://github.com/twbs"</script>'
    assert page_socials(html)["githubs"] == [("https://github.com/acme", True)]
