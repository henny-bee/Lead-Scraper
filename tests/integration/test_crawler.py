import asyncio
import time
from pathlib import Path

import httpx
import pytest
import respx

from leadscraper.crawler.fetcher import SKIP_NON_HTML, SKIP_PAGE_LIMIT, SKIP_ROBOTS, SKIP_TOO_LARGE, Fetcher
from leadscraper.crawler.pages import crawl_site
from leadscraper.crawler.robots import RobotsCache
from leadscraper.crawler.website import HostGuard, normalize_website
from leadscraper.observability import metrics
from leadscraper.services.resolver.profile import ProfileBuilder, load_contact_pages
from leadscraper.settings import load_settings

pytestmark = pytest.mark.anyio

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
CONTACT = load_contact_pages()
DE = ProfileBuilder().get("DE")
UA = "LeadScraperBot/0.3 (+https://your-domain.de/bot)"
HTML = {"content-type": "text/html; charset=utf-8"}


def read(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


async def public(_host: str) -> list[str]:
    return ["93.184.216.34"]                       # fake DNS: everything public, no network


def make_fetcher(client: httpx.AsyncClient, delay: str = "0", **env: str) -> Fetcher:
    settings = load_settings({"CRAWLER_PER_DOMAIN_DELAY_S": delay, **env})
    fetcher = Fetcher(settings, client, HostGuard(public))
    RobotsCache(fetcher).attach()
    return fetcher


async def crawl(fetcher: Fetcher, url: str, save_dir: Path | None = None):
    return await crawl_site(fetcher, normalize_website(url), keywords=DE.contact_keywords,
                            fallback_paths=CONTACT.fallback_paths,
                            legal_markers=CONTACT.legal_markers, save_dir=save_dir)


def sample(name: str, labels: dict) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


async def test_german_site_impressum_found_and_saved(tmp_path: Path) -> None:
    base = "https://www.muster-maschinenbau-example.de"
    before_200 = sample("crawl_requests_total", {"status_code": "200"})
    before_count = sample("crawl_duration_seconds_count", {})
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        home = mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML,
                                                                      text=read("de_home.html")))
        imp = mock.get(f"{base}/impressum/").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html><body>Impressum info@muster-maschinenbau-example.de</body></html>"))
        kon = mock.get(f"{base}/ueber-uns/kontakt").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html><body>Kontakt</body></html>"))
        async with httpx.AsyncClient() as client:
            result = await crawl(make_fetcher(client), base, save_dir=tmp_path / "crawl")
    assert [p.kind for p in result.pages] == ["home", "legal", "contact"]
    assert result.pages[1].is_legal and not result.pages[2].is_legal
    assert result.lang == "de" and result.website.origin == base
    assert home.call_count == imp.call_count == kon.call_count == 1
    assert home.calls[0].request.headers["user-agent"] == UA
    assert len(list((tmp_path / "crawl").glob("*.html"))) == 3
    assert sample("crawl_requests_total", {"status_code": "200"}) >= before_200 + 3
    assert sample("crawl_duration_seconds_count", {}) >= before_count + 4   # incl. robots.txt


async def test_fallback_impressum_when_no_link() -> None:
    base = "https://demo-logistik-example.de"
    with respx.mock() as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML,
                                                               text=read("no_links_home.html")))
        imp = mock.get(f"{base}/impressum").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html>Impressum</html>"))
        other = mock.get(url__regex=rf"{base}/(mentions-legales|aviso-legal|contact)").mock(
            return_value=httpx.Response(404, headers=HTML))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            result = await crawl(fetcher, base)
    assert imp.call_count == 1
    assert [p.kind for p in result.pages] == ["home", "fallback"]
    assert result.pages[1].is_legal
    assert other.call_count == 3                    # homepage + 4 fallbacks = 5 page limit
    assert fetcher.pages["demo-logistik-example.de"] == 5


async def test_never_more_than_five_pages_per_domain() -> None:
    base = "https://viele-links-example.de"
    links = "".join(f'<a href="/kontakt-{i}">Kontakt {i}</a>' for i in range(10))
    with respx.mock() as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML,
                                                               text=f"<html>{links}</html>"))
        sub = mock.get(url__regex=rf"{base}/kontakt-\d+").mock(
            return_value=httpx.Response(200, headers=HTML, text="<html>x</html>"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            result = await crawl(fetcher, base)
            extra = await fetcher.fetch(f"{base}/kontakt-9")
    assert len(result.pages) == 5 and sub.call_count == 4
    assert extra.skipped == SKIP_PAGE_LIMIT and sub.call_count == 4


async def test_robots_disallowed_paths_not_fetched() -> None:
    base = "https://www.muster-maschinenbau-example.de"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(
            200, text="User-agent: *\nDisallow: /impressum\n", headers={"content-type": "text/plain"}))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=read("de_home.html")))
        imp = mock.get(f"{base}/impressum/").mock(return_value=httpx.Response(200, headers=HTML, text="x"))
        mock.get(f"{base}/ueber-uns/kontakt").mock(return_value=httpx.Response(200, headers=HTML, text="k"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            result = await crawl(fetcher, base)
            direct = await fetcher.fetch(f"{base}/impressum/")
    assert imp.call_count == 0 and direct.skipped == SKIP_ROBOTS
    assert [p.kind for p in result.pages] == ["home", "contact"]


async def test_robots_disallow_all_and_5xx() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://blocked-example.de/robots.txt").mock(return_value=httpx.Response(
            200, text="User-agent: LeadScraperBot\nDisallow: /\n"))
        mock.get("https://down-example.de/robots.txt").mock(return_value=httpx.Response(503))
        home = mock.get(url__regex=r"https://(blocked|down)\.de/$").mock(
            return_value=httpx.Response(200, headers=HTML, text="<html></html>"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            r1 = await crawl(fetcher, "https://blocked-example.de")
            r2 = await crawl(fetcher, "https://down-example.de")
    assert home.call_count == 0
    assert r1.skipped == SKIP_ROBOTS and r2.skipped == SKIP_ROBOTS and not r1.pages


async def test_crawl_delay_from_robots_raises_domain_delay() -> None:
    with respx.mock() as mock:
        mock.get("https://slow-example.de/robots.txt").mock(return_value=httpx.Response(
            200, text="User-agent: *\nCrawl-delay: 99\n"))
        mock.get("https://slow-example.de/").mock(return_value=httpx.Response(200, headers=HTML, text="<html/>"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            await fetcher.fetch("https://slow-example.de/")
    assert fetcher.domain_delay["slow-example.de"] == 10.0          # capped (ROBOTS_MAX_CRAWL_DELAY_S)


async def test_same_domain_requests_respect_delay() -> None:
    stamps: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        stamps.append(time.monotonic())
        return httpx.Response(200, headers=HTML, text="<html></html>")

    with respx.mock() as mock:
        mock.get(url__regex=r"https://(www\.)?firma-example\.de/robots\.txt").mock(
            return_value=httpx.Response(404))
        mock.get(url__regex=r"https://(www\.)?firma-example\.de/p\d").mock(side_effect=record)
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client, delay="0.25")
            await asyncio.gather(fetcher.fetch("https://firma-example.de/p1"),
                                 fetcher.fetch("https://www.firma-example.de/p2"),   # same registered domain
                                 fetcher.fetch("https://firma-example.de/p3"))
    assert len(stamps) == 3
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.24


async def test_other_domains_not_delayed_by_each_other() -> None:
    with respx.mock() as mock:
        mock.get(url__regex=r"https://[ab]-example\.de/robots\.txt").mock(return_value=httpx.Response(404))
        mock.get(url__regex=r"https://[ab]-example\.de/$").mock(
            return_value=httpx.Response(200, headers=HTML, text="<html/>"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client, delay="1")
            t0 = time.monotonic()
            await asyncio.gather(fetcher.fetch("https://a-example.de/"), fetcher.fetch("https://b-example.de/"))
    assert time.monotonic() - t0 < 2.0       # a-example.de and b-example.de each: robots + page (1 s gap each)


async def test_large_response_aborted_and_non_html_skipped() -> None:
    big = b"<html>" + b"x" * (3 * 1024 * 1024) + b"</html>"
    with respx.mock() as mock:
        mock.get("https://big-example.de/robots.txt").mock(return_value=httpx.Response(404))
        mock.get("https://big-example.de/declared").mock(return_value=httpx.Response(
            200, headers={**HTML, "content-length": str(len(big))}, content=big))
        mock.get("https://big-example.de/streamed").mock(return_value=httpx.Response(
            200, headers=HTML, stream=httpx.ByteStream(big)))
        mock.get("https://big-example.de/logo.png").mock(return_value=httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"\x89PNG"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            declared = await fetcher.fetch("https://big-example.de/declared")
            streamed = await fetcher.fetch("https://big-example.de/streamed")
            png = await fetcher.fetch("https://big-example.de/logo.png")
    assert declared.skipped == SKIP_TOO_LARGE and declared.html is None
    assert streamed.skipped == SKIP_TOO_LARGE and streamed.html is None
    assert png.skipped == SKIP_NON_HTML and png.html is None


async def test_redirect_on_homepage_confirms_website() -> None:
    with respx.mock() as mock:
        mock.get(url__regex=r"https://(www\.)?firma-example\.de/robots\.txt").mock(return_value=httpx.Response(404))
        mock.get("https://firma-example.de/").mock(return_value=httpx.Response(
            301, headers={"location": "https://www.firma-example.de/"}))
        mock.get("https://www.firma-example.de/").mock(return_value=httpx.Response(
            200, headers=HTML, text='<html lang="de"><a href="/impressum">Impressum</a></html>'))
        mock.get("https://www.firma-example.de/impressum").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html>imp</html>"))
        async with httpx.AsyncClient() as client:
            result = await crawl(make_fetcher(client), "firma-example.de")
    assert result.website.origin == "https://www.firma-example.de"
    assert [p.url for p in result.pages] == ["https://www.firma-example.de/", "https://www.firma-example.de/impressum"]


async def test_https_failure_falls_back_to_http() -> None:
    with respx.mock() as mock:
        mock.get("https://alt-example.de/robots.txt").mock(side_effect=httpx.ConnectError("no tls"))
        mock.get("http://alt-example.de/robots.txt").mock(return_value=httpx.Response(404))
        mock.get("http://alt-example.de/").mock(return_value=httpx.Response(200, headers=HTML, text="<html/>"))
        fallback = mock.get(url__regex=r"http://alt-example\.de/(impressum|mentions-legales|aviso-legal|contact)$").mock(
            return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            result = await crawl(fetcher, "alt-example.de")
    assert result.skipped is None and result.website.origin == "http://alt-example.de"
    assert fallback.call_count == 4                  # fallback paths on the http origin
