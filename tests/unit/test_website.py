import httpx
import pytest
import respx

from leadscraper.constants import MAX_REDIRECTS
from leadscraper.crawler.fetcher import SKIP_PAGE_LIMIT, Fetcher
from leadscraper.crawler.website import (
    SKIP_INVALID,
    SKIP_NO_WEBSITE,
    SKIP_PRIVATE,
    SKIP_REDIRECTS,
    SKIP_SOCIAL,
    HostGuard,
    WebsiteRejected,
    is_public_ip,
    normalize_website,
    registered_domain,
    website_for,
)
from leadscraper.domain.models import CompanyCandidate
from leadscraper.settings import load_settings

UA = "LeadScraperBot/test"


def fake_resolver(table: dict[str, list[str]]):
    calls: list[str] = []

    async def resolve(host: str) -> list[str]:
        calls.append(host)
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def test_consistent_normalisation() -> None:
    a = normalize_website("example.de")
    b = normalize_website("http://www.example.de/")
    c = normalize_website("https://example.de/?utm_source=x")
    assert a.url == c.url == "https://example.de/" and a.origin == "https://example.de"
    assert b.url == "http://www.example.de/" and b.origin == "http://www.example.de"
    assert a.registered_domain == b.registered_domain == c.registered_domain == "example.de"


@pytest.mark.parametrize("raw,url", [
    ("HTTPS://WWW.Firma-example.DE:443/Kontakt#top", "https://www.firma-example.de/Kontakt"),
    ("http://firma-example.de:80", "http://firma-example.de/"),
    ("firma-example.de:8080/x", "https://firma-example.de:8080/x"),
    ("https://firma-example.de/?utm_medium=a&id=5&gclid=z&fbclid=q", "https://firma-example.de/?id=5"),
    ("  https://a-firma-example.de ; https://b-firma-example.de  ", "https://a-firma-example.de/"),
    ("//firma-example.de/path", "https://firma-example.de/path"),
    ("https://übungs-bau-example.de", "https://xn--bungs-bau-example-12b.de/"),
    ("https://shop.example.co.uk/de", "https://shop.example.co.uk/de"),
])
def test_normalise_cases(raw: str, url: str) -> None:
    assert normalize_website(raw).url == url


@pytest.mark.parametrize("raw,reason", [
    (None, SKIP_NO_WEBSITE), ("", SKIP_NO_WEBSITE), ("   ", SKIP_NO_WEBSITE),
    ("mailto:info@firma-example.de", SKIP_INVALID), ("ftp://firma-example.de", SKIP_INVALID),
    ("javascript:alert(1)", SKIP_INVALID), ("tel:+4989123", SKIP_INVALID),
    ("http://localhost:8000", SKIP_INVALID), ("http://intranet/", SKIP_INVALID),
    ("https://", SKIP_INVALID), ("http://[::1", SKIP_INVALID),
    ("http://127.0.0.1/", SKIP_PRIVATE), ("http://10.0.0.5", SKIP_PRIVATE),
    ("http://169.254.169.254/latest/meta-data", SKIP_PRIVATE), ("http://[::1]/", SKIP_PRIVATE),
    ("http://192.168.1.1", SKIP_PRIVATE), ("http://100.64.0.1", SKIP_PRIVATE),
    ("https://www.facebook.com/firma", SKIP_SOCIAL), ("instagram.com/firma", SKIP_SOCIAL),
    ("https://www.linkedin.com/company/x", SKIP_SOCIAL), ("https://wa.me/49123", SKIP_SOCIAL),
    ("https://www.yelp.de/biz/x", SKIP_SOCIAL), ("https://www.tripadvisor.co.uk/x", SKIP_SOCIAL),
    ("https://www.gelbeseiten.de/gsbiz/x", SKIP_SOCIAL), ("https://maps.google.com/?cid=1", SKIP_SOCIAL),
    ("https://linktr.ee/firma", SKIP_SOCIAL),
])
def test_rejected(raw, reason) -> None:
    with pytest.raises(WebsiteRejected) as exc:
        normalize_website(raw)
    assert exc.value.reason == reason


def test_public_ip_literal_allowed_and_ip_helpers() -> None:
    site = normalize_website("http://93.184.216.34/")
    assert site.origin == "http://93.184.216.34" and site.registered_domain == "93.184.216.34"
    assert is_public_ip("8.8.8.8") and not is_public_ip("::ffff:127.0.0.1")
    assert not is_public_ip("224.0.0.1")


def test_candidate_website_from_osm_tags() -> None:
    c = CompanyCandidate(name="X", source="osm", source_ref="node/1", website="www.firma-example.de")
    assert website_for(c).origin == "https://www.firma-example.de"
    with pytest.raises(WebsiteRejected):
        website_for(CompanyCandidate(name="Y", source="osm", source_ref="node/2"))
    assert registered_domain("https://www.shop.firma-example.de/impressum") == "firma-example.de"


@pytest.mark.anyio
async def test_host_guard_resolves_and_caches() -> None:
    resolve = fake_resolver({"firma-example.de": ["93.184.216.34"], "evil-example.de": ["93.184.216.34", "10.0.0.1"],
                             "rebind-example.de": ["127.0.0.1"]})
    guard = HostGuard(resolve)
    assert await guard.is_public("firma-example.de") and await guard.is_public("FIRMA-EXAMPLE.de")
    assert resolve.calls == ["firma-example.de"]                       # cached per job
    assert not await guard.is_public("evil-example.de")                # any private address → reject
    assert not await guard.is_public("rebind-example.de")
    assert not await guard.is_public("nxdomain-example.de")
    assert await guard.is_public("8.8.8.8") and not await guard.is_public("10.1.1.1")


# --- redirect / SSRF scenarios through the Fetcher (the only crawl request path, C11) ------------
HTML = {"content-type": "text/html"}


def make_fetcher(client: httpx.AsyncClient, resolve, **env: str) -> Fetcher:
    settings = load_settings({"CRAWLER_PER_DOMAIN_DELAY_S": "0", "CRAWLER_USER_AGENT": UA, **env})
    return Fetcher(settings, client, HostGuard(resolve))


@pytest.mark.anyio
async def test_redirect_chain_final_url_becomes_website() -> None:
    resolve = fake_resolver({"example.de": ["93.184.216.34"], "www.example.de": ["93.184.216.34"]})
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://example.de/").mock(return_value=httpx.Response(
            301, headers={"Location": "http://www.example.de/"}))
        mock.get("http://www.example.de/").mock(return_value=httpx.Response(
            302, headers={"Location": "https://www.example.de/de/?utm_source=x"}))
        final = mock.get("https://www.example.de/de/").mock(
            return_value=httpx.Response(200, headers=HTML, text="<html>ok</html>"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client, resolve)
            res = await fetcher.fetch(normalize_website("example.de"))
    assert res.ok and res.final_url == "https://www.example.de/de/"
    assert res.final_site.origin == "https://www.example.de"      # stored as `website`
    assert res.final_site.registered_domain == "example.de"
    assert final.calls[0].request.headers["user-agent"] == UA
    assert fetcher.pages["example.de"] == 3                        # every hop is a polite request


@pytest.mark.anyio
async def test_redirect_to_private_address_blocked() -> None:
    resolve = fake_resolver({"firma-example.de": ["93.184.216.34"], "internal.firma-example.de": ["10.0.0.7"]})
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get("https://firma-example.de/").mock(return_value=httpx.Response(
            302, headers={"Location": "https://internal.firma-example.de/admin"}))
        async with httpx.AsyncClient() as client:
            res = await make_fetcher(client, resolve).fetch("firma-example.de")
    assert res.skipped == SKIP_PRIVATE and res.html is None


@pytest.mark.anyio
async def test_redirect_to_metadata_ip_and_social_blocked() -> None:
    resolve = fake_resolver({"firma-example.de": ["93.184.216.34"]})
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get("https://firma-example.de/").mock(return_value=httpx.Response(
            302, headers={"Location": "http://169.254.169.254/"}))
        async with httpx.AsyncClient() as client:
            res = await make_fetcher(client, resolve).fetch("firma-example.de")
    assert res.skipped == SKIP_PRIVATE
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get("https://firma-example.de/").mock(return_value=httpx.Response(
            301, headers={"Location": "https://www.facebook.com/firma"}))
        async with httpx.AsyncClient() as client:
            res = await make_fetcher(client, resolve).fetch("firma-example.de")
    assert res.skipped == SKIP_SOCIAL


@pytest.mark.anyio
async def test_direct_private_host_never_requested() -> None:
    resolve = fake_resolver({"rebind-example.de": ["127.0.0.1"]})
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.get("https://rebind-example.de/").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            res = await make_fetcher(client, resolve).fetch("rebind-example.de")
    assert res.skipped == SKIP_PRIVATE and route.call_count == 0


@pytest.mark.anyio
async def test_redirect_loop_limited() -> None:
    resolve = fake_resolver({"loop-example.de": ["93.184.216.34"]})
    with respx.mock() as mock:
        route = mock.get("https://loop-example.de/").mock(return_value=httpx.Response(
            302, headers={"Location": "/"}))
        async with httpx.AsyncClient() as client:
            res = await make_fetcher(client, resolve, CRAWLER_MAX_PAGES_PER_DOMAIN="50").fetch("loop-example.de")
            limited = await make_fetcher(client, resolve).fetch("loop-example.de")
    assert res.skipped == SKIP_REDIRECTS
    assert route.call_count == MAX_REDIRECTS + 1 + 5              # + second fetcher: page limit 5
    assert limited.skipped == SKIP_PAGE_LIMIT                     # default page cap bounds it first
