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


# --- per-site budget, fail-fast, robots.txt timeout -------------------------------------------------
THREE_LINKS = ('<html lang="de"><a href="/impressum">Impressum</a> <a href="/kontakt">Kontakt</a> '
               '<a href="/ueber-uns">Über uns</a></html>')


def honours_read_timeout(needed_s: float, response: httpx.Response):
    """Respx transports ignore timeouts; this side effect behaves like a server that answers after
    ``needed_s`` seconds: a request whose read timeout is shorter gets ``httpx.ReadTimeout``."""
    def side_effect(request: httpx.Request) -> httpx.Response:
        read = (request.extensions.get("timeout") or {}).get("read")
        if read is not None and read < needed_s:
            raise httpx.ReadTimeout("simulated slow server", request=request)
        return response
    return side_effect


async def test_consecutive_failures_stop_site() -> None:
    base = "https://wackel-example.de"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=THREE_LINKS))
        pages = mock.get(url__regex=rf"{base}/(impressum|kontakt|ueber-uns)$").mock(
            side_effect=httpx.ReadTimeout("timed out"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            result = await crawl_site(fetcher, normalize_website(base), keywords=DE.contact_keywords,
                                      fallback_paths=CONTACT.fallback_paths,
                                      legal_markers=CONTACT.legal_markers, max_consecutive_failures=2)
    assert pages.call_count == 2                       # 2 timeouts in a row → no third request
    assert [p.kind for p in result.pages] == ["home"] and result.skipped is None


async def test_site_deadline_returns_pages_collected_so_far() -> None:
    base = "https://haenger-example.de"

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, headers=HTML, text="<html>late</html>")

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=THREE_LINKS))
        mock.get(f"{base}/impressum").mock(side_effect=hang)
        kontakt = mock.get(f"{base}/kontakt").mock(return_value=httpx.Response(200, headers=HTML, text="k"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            t0 = time.monotonic()
            result = await crawl_site(fetcher, normalize_website(base), keywords=DE.contact_keywords,
                                      fallback_paths=CONTACT.fallback_paths,
                                      legal_markers=CONTACT.legal_markers, deadline=time.monotonic() + 0.5)
    assert time.monotonic() - t0 < 5
    assert [p.kind for p in result.pages] == ["home"] and result.skipped is None
    assert kontakt.call_count == 0


async def test_home_page_over_budget_is_skipped_site_budget() -> None:
    base = "https://traege-example.de"

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, headers=HTML, text="<html/>")

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(side_effect=hang)
        async with httpx.AsyncClient() as client:
            result = await crawl_site(make_fetcher(client), normalize_website(base),
                                      keywords=DE.contact_keywords, fallback_paths=CONTACT.fallback_paths,
                                      legal_markers=CONTACT.legal_markers, deadline=time.monotonic() + 0.3)
    assert result.skipped == "site_budget" and not result.pages


async def test_slow_robots_txt_still_parsed() -> None:
    """Robots.txt keeps the full ``CRAWLER_HTTP_TIMEOUT_S`` read timeout: a robots.txt that answers
    after the page read timeout (but in time) is parsed, not turned into a disallow-all."""
    from leadscraper import constants as C
    base = "https://langsam-robots-example.de"
    rules = httpx.Response(200, text="User-agent: *\nDisallow: /impressum\n",
                           headers={"content-type": "text/plain"})
    needed = (C.CRAWLER_READ_TIMEOUT_S + C.CRAWLER_HTTP_TIMEOUT_S) / 2       # 15 s: > page read timeout
    timeout = httpx.Timeout(C.CRAWLER_HTTP_TIMEOUT_S, connect=C.CRAWLER_CONNECT_TIMEOUT_S,
                            read=C.CRAWLER_READ_TIMEOUT_S)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(side_effect=honours_read_timeout(needed, rules))
        home = mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=THREE_LINKS))
        imp = mock.get(f"{base}/impressum").mock(return_value=httpx.Response(200, headers=HTML, text="i"))
        kon = mock.get(f"{base}/kontakt").mock(return_value=httpx.Response(200, headers=HTML, text="k"))
        mock.get(f"{base}/ueber-uns").mock(return_value=httpx.Response(200, headers=HTML, text="u"))
        async with httpx.AsyncClient(timeout=timeout) as client:
            result = await crawl(make_fetcher(client), base)
    assert home.call_count == 1 and kon.call_count == 1          # rules applied, not disallow-all
    assert imp.call_count == 0
    assert result.skipped is None


# --- early-exit predicate ------------------------------------------------------------------------------
async def test_done_predicate_stops_after_matching_page() -> None:
    base = "https://frueh-crawl-example.de"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=THREE_LINKS))
        imp = mock.get(f"{base}/impressum").mock(return_value=httpx.Response(200, headers=HTML, text="i"))
        rest = mock.get(url__regex=rf"{base}/(kontakt|ueber-uns)$").mock(
            return_value=httpx.Response(200, headers=HTML, text="k"))
        async with httpx.AsyncClient() as client:
            result = await crawl_site(make_fetcher(client), normalize_website(base),
                                      keywords=DE.contact_keywords, fallback_paths=CONTACT.fallback_paths,
                                      legal_markers=CONTACT.legal_markers,
                                      done=lambda pages: any(p.is_legal for p in pages))
    assert imp.call_count == 1 and rest.call_count == 0
    assert [p.kind for p in result.pages] == ["home", "legal"]


# --- fallback paths per missing page kind -------------------------------------------------------------
BY_KIND = ProfileBuilder().fallback_paths_for(DE.languages)


async def crawl_by_kind(fetcher: Fetcher, url: str):
    return await crawl_site(fetcher, normalize_website(url), keywords=DE.contact_keywords,
                            fallback_paths=CONTACT.fallback_paths, legal_markers=CONTACT.legal_markers,
                            fallback_by_kind=BY_KIND, contact_markers=CONTACT.contact_markers)


async def test_contact_fallback_when_only_legal_link() -> None:
    base = "https://nur-impressum-example.de"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(
            200, headers=HTML, text='<html lang="de"><a href="/impressum">Impressum</a></html>'))
        mock.get(f"{base}/impressum").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html><body>Firma GmbH, 28195 Bremen</body></html>"))
        kontakt = mock.get(f"{base}/kontakt").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html><body>info@nur-impressum-example.de</body></html>"))
        async with httpx.AsyncClient() as client:
            result = await crawl_by_kind(make_fetcher(client), base)
    assert kontakt.call_count == 1
    assert [(p.kind, p.is_legal) for p in result.pages] == [("home", False), ("legal", True),
                                                             ("fallback", False)]
    assert "info@nur-impressum-example.de" in result.pages[-1].html


async def test_legal_fallback_when_only_contact_link() -> None:
    base = "https://nur-kontakt-example.de"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(
            200, headers=HTML, text='<html lang="de"><a href="/kontakt">Kontakt</a></html>'))
        imp = mock.get(f"{base}/impressum").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html><body>Impressum info@nur-kontakt-example.de</body></html>"))
        kon = mock.get(f"{base}/kontakt").mock(return_value=httpx.Response(200, headers=HTML, text="k"))
        async with httpx.AsyncClient() as client:
            result = await crawl_by_kind(make_fetcher(client), base)
    assert imp.call_count == 1 and kon.call_count == 1
    assert [(p.kind, p.is_legal) for p in result.pages] == [("home", False), ("fallback", True),
                                                             ("contact", False)]


async def test_other_matches_fetched_after_contact() -> None:
    base = "https://reihenfolge-example.de"
    home = ('<html lang="de"><a href="/ueber-uns">Über uns</a> <a href="/kontakt">Kontakt</a> '
            '<a href="/impressum">Impressum</a></html>')
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=home))
        mock.get(url__regex=rf"{base}/(impressum|kontakt|ueber-uns)$").mock(
            return_value=httpx.Response(200, headers=HTML, text="<html>x</html>"))
        async with httpx.AsyncClient() as client:
            result = await crawl_by_kind(make_fetcher(client), base)
    assert [p.kind for p in result.pages] == ["home", "legal", "contact", "other"]


async def test_fallbacks_respect_page_budget() -> None:
    base = "https://viele-seiten-example.de"
    home = "".join(f'<a href="/ueber-uns-{i}">Über uns {i}</a>' for i in range(8))
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=f"<html>{home}</html>"))
        pages = mock.get(url__regex=rf"{base}/.+").mock(
            return_value=httpx.Response(404, headers=HTML, text="<html>404</html>"))
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            await crawl_by_kind(fetcher, base)
    assert fetcher.pages["viele-seiten-example.de"] <= 5            # home + probes, all counted
    probed = [c.request.url.path for c in pages.calls]
    # budget order: legal probes while > 1 page stays, then the contact probes, then "other"
    assert probed == ["/impressum", "/imprint", "/legal-notice", "/kontakt"]


# --- seed probing (www. flip) + Accept-Language ------------------------------------------------------
def fetcher_with_dns(client: httpx.AsyncClient, answers: dict[str, list[str] | None], **kw) -> Fetcher:
    async def resolve(host: str) -> list[str]:
        value = answers.get(host, ["93.184.216.34"])
        if value is None:
            raise OSError("NXDOMAIN")
        return value
    fetcher = Fetcher(load_settings({"CRAWLER_PER_DOMAIN_DELAY_S": "0"}), client, HostGuard(resolve), **kw)
    RobotsCache(fetcher).attach()
    return fetcher


async def crawl_seed(fetcher: Fetcher, url: str):
    return await crawl_site(fetcher, normalize_website(url), keywords=DE.contact_keywords,
                            fallback_paths=CONTACT.fallback_paths, legal_markers=CONTACT.legal_markers,
                            seed_variants=True)


async def test_bare_domain_unresolvable_www_resolves() -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get("https://www.nur-www-example.de/robots.txt").mock(return_value=httpx.Response(404))
        home = mock.get("https://www.nur-www-example.de/").mock(return_value=httpx.Response(
            200, headers=HTML, text='<html lang="de"><a href="/impressum">Impressum</a></html>'))
        mock.get("https://www.nur-www-example.de/impressum").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html>info@nur-www-example.de</html>"))
        async with httpx.AsyncClient() as client:
            fetcher = fetcher_with_dns(client, {"nur-www-example.de": None})
            result = await crawl_seed(fetcher, "nur-www-example.de")
    assert home.call_count == 1 and result.skipped is None
    assert result.website.origin == "https://www.nur-www-example.de"
    assert [p.kind for p in result.pages] == ["home", "legal"]
    assert "nur-www-example.de" in fetcher.guard.unresolvable


async def test_www_host_5xx_bare_host_ok() -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(url__regex=r"https://(www\.)?kaputt-www-example\.de/robots\.txt").mock(
            return_value=httpx.Response(404))
        www = mock.get("https://www.kaputt-www-example.de/").mock(return_value=httpx.Response(525))
        bare = mock.get("https://kaputt-www-example.de/").mock(return_value=httpx.Response(
            200, headers=HTML, text="<html>ok</html>"))
        mock.get(url__regex=r"https://kaputt-www-example\.de/.+").mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            result = await crawl_seed(fetcher_with_dns(client, {}), "https://www.kaputt-www-example.de")
    assert www.call_count == 1 and bare.call_count == 1
    assert result.skipped is None and result.website.origin == "https://kaputt-www-example.de"


async def test_503_is_not_flipped() -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(url__regex=r"https://(www\.)?besetzt-example\.de/robots\.txt").mock(
            return_value=httpx.Response(404))
        mock.get("https://besetzt-example.de/").mock(return_value=httpx.Response(503))
        www = mock.get("https://www.besetzt-example.de/").mock(return_value=httpx.Response(200, headers=HTML))
        async with httpx.AsyncClient() as client:
            result = await crawl_seed(fetcher_with_dns(client, {}), "https://besetzt-example.de")
    assert www.call_count == 0 and result.skipped == "http_error"


async def test_flipped_host_still_ssrf_checked() -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        anything = mock.get(url__regex=r"https?://.*").mock(return_value=httpx.Response(200, headers=HTML))
        async with httpx.AsyncClient() as client:
            fetcher = fetcher_with_dns(client, {"privat-flip-example.de": None,
                                                "www.privat-flip-example.de": ["10.0.0.1"]})
            result = await crawl_seed(fetcher, "https://privat-flip-example.de")
    assert anything.call_count == 0 and result.skipped == "non_public_address"


async def test_seed_order_www_flip_then_http() -> None:
    order: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        order.append(str(request.url))
        raise httpx.ConnectError("refused", request=request)

    def ok(request: httpx.Request) -> httpx.Response:
        order.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers=HTML, text="<html/>") if request.url.path == "/" else httpx.Response(404)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(url__regex=r"https://.*").mock(side_effect=refuse)
        mock.get(url__regex=r"http://.*").mock(side_effect=ok)
        async with httpx.AsyncClient() as client:
            result = await crawl_seed(fetcher_with_dns(client, {}), "alt-flip-example.de")
    assert order[:3] == ["https://alt-flip-example.de/robots.txt",
                         "https://www.alt-flip-example.de/robots.txt",
                         "http://alt-flip-example.de/robots.txt"]
    assert result.skipped is None and result.website.origin == "http://alt-flip-example.de"


async def test_accept_language_header_sent() -> None:
    from leadscraper.crawler.fetcher import accept_language
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get("https://sprache-example.de/robots.txt").mock(return_value=httpx.Response(404))
        home = mock.get("https://sprache-example.de/").mock(return_value=httpx.Response(200, headers=HTML, text="<html/>"))
        async with httpx.AsyncClient() as client:
            fetcher = fetcher_with_dns(client, {}, accept_language=accept_language(("de",), "DE"))
            await fetcher.fetch("https://sprache-example.de/")
    assert home.calls[0].request.headers["accept-language"] == "de-DE,de;q=0.9,en;q=0.8"
    assert accept_language(("de", "fr", "it"), "CH") == "de-CH,de;q=0.9,fr;q=0.8,it;q=0.7,en;q=0.6"
    assert accept_language(("en",), "GB") == "en-GB,en;q=0.9"


# --- correction: budget order, skip probes once a page of the kind is found ----------------------------
async def probe_run(base: str, pages: dict[str, httpx.Response], home: str = "<html></html>") -> list[str]:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
        mock.get(f"{base}/").mock(return_value=httpx.Response(200, headers=HTML, text=home))

        def page(request: httpx.Request) -> httpx.Response:
            return pages.get(request.url.path, httpx.Response(404, headers=HTML, text="<html>404</html>"))

        route = mock.get(url__regex=rf"{base}/.+").mock(side_effect=page)
        async with httpx.AsyncClient() as client:
            result = await crawl_by_kind(make_fetcher(client), base)
    return [c.request.url.path for c in route.calls], result


async def test_contact_page_reserved_when_no_legal_found() -> None:
    kontakt = httpx.Response(200, headers=HTML, text="<html><body>info@reserve-example.de</body></html>")
    probed, result = await probe_run("https://reserve-example.de", {"/kontakt": kontakt})
    assert probed == ["/impressum", "/imprint", "/legal-notice", "/kontakt"]   # the last page is /kontakt
    assert "info@reserve-example.de" in result.pages[-1].html and len(probed) + 1 == 5


async def test_remaining_legal_fallbacks_skipped_after_legal_found() -> None:
    imp = httpx.Response(200, headers=HTML, text="<html>Impressum</html>")
    probed, result = await probe_run("https://legal-da-example.de", {"/impressum": imp})
    assert "/imprint" not in probed and "/legal-notice" not in probed
    assert probed[0] == "/impressum" and result.pages[1].is_legal


async def test_remaining_contact_fallbacks_skipped_after_contact_found() -> None:
    kon = httpx.Response(200, headers=HTML, text="<html>Kontakt</html>")
    home = '<html lang="de"><a href="/impressum">Impressum</a></html>'
    imp = httpx.Response(200, headers=HTML, text="<html>Impressum</html>")
    probed, _ = await probe_run("https://kontakt-da-example.de", {"/impressum": imp, "/kontakt": kon}, home)
    assert probed == ["/impressum", "/kontakt"]                     # no /contact, no /contact-us


# --- sitemap fallback --------------------------------------------------------------------------------
XML = {"content-type": "application/xml"}


def urlset(base: str, *paths: str) -> str:
    return "<urlset>" + "".join(f"<url><loc>{base}{p}</loc></url>" for p in paths) + "</urlset>"


async def crawl_sitemap(fetcher: Fetcher, url: str):
    return await crawl_site(fetcher, normalize_website(url), keywords=DE.contact_keywords,
                            fallback_paths=CONTACT.fallback_paths, legal_markers=CONTACT.legal_markers,
                            fallback_by_kind=BY_KIND, contact_markers=CONTACT.contact_markers,
                            use_sitemap=True)


def site_routes(mock: respx.MockRouter, base: str, pages: dict[str, httpx.Response], robots: str | None = None):
    mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(
        200, text=robots, headers={"content-type": "text/plain"}) if robots else httpx.Response(404))

    def page(request: httpx.Request) -> httpx.Response:
        return pages.get(request.url.path, httpx.Response(404, headers=HTML, text="<html>404</html>"))

    return mock.get(url__regex=rf"{base}/.*").mock(side_effect=page)


async def test_sitemap_finds_nonstandard_impressum() -> None:
    base = "https://versteckt-example.de"
    pages = {"/": httpx.Response(200, headers=HTML, text='<html lang="de"><a href="/leistungen">Leistungen</a></html>'),
             "/sitemap.xml": httpx.Response(200, headers=XML, text=urlset(base, "/", "/leistungen",
                                                                          "/rechtliches/impressum.html")),
             "/rechtliches/impressum.html": httpx.Response(200, headers=HTML,
                                                           text="<html>info@versteckt-example.de</html>")}
    with respx.mock(assert_all_called=False) as mock:
        route = site_routes(mock, base, pages)
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            result = await crawl_sitemap(fetcher, base)
    paths = [c.request.url.path for c in route.calls]
    assert paths == ["/", "/impressum", "/sitemap.xml", "/rechtliches/impressum.html", "/kontakt"]
    assert fetcher.pages["versteckt-example.de"] == 5
    legal = [p for p in result.pages if p.is_legal]
    assert legal and "info@versteckt-example.de" in legal[0].html and legal[0].kind == "legal"


async def test_sitemap_index_one_level() -> None:
    base = "https://index-example.de"
    index = ("<sitemapindex>" + f"<sitemap><loc>{base}/post-sitemap.xml</loc></sitemap>"
             + f"<sitemap><loc>{base}/page-sitemap.xml</loc></sitemap></sitemapindex>")
    pages = {"/": httpx.Response(200, headers=HTML, text="<html></html>"),
             "/sitemap_index.xml": httpx.Response(200, headers=XML, text=index),
             "/page-sitemap.xml": httpx.Response(200, headers=XML, text=urlset(base, "/ueber-uns/impressum")),
             "/ueber-uns/impressum": httpx.Response(200, headers=HTML, text="<html>info@index-example.de</html>")}
    with respx.mock(assert_all_called=False) as mock:
        route = site_routes(mock, base, pages, robots=f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap_index.xml\n")
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            result = await crawl_sitemap(fetcher, base)
    paths = [c.request.url.path for c in route.calls]
    assert "/post-sitemap.xml" not in paths                           # one child, "page" preferred
    assert paths[:5] == ["/", "/impressum", "/sitemap_index.xml", "/page-sitemap.xml", "/ueber-uns/impressum"]
    assert fetcher.pages["index-example.de"] <= 5
    assert any(p.is_legal and "info@index-example.de" in p.html for p in result.pages)


async def test_sitemap_not_fetched_when_legal_found() -> None:
    base = "https://mit-link-example.de"
    pages = {"/": httpx.Response(200, headers=HTML, text='<html><a href="/impressum">Impressum</a></html>'),
             "/impressum": httpx.Response(200, headers=HTML, text="<html>Impressum</html>"),
             "/sitemap.xml": httpx.Response(200, headers=XML, text=urlset(base, "/impressum"))}
    with respx.mock(assert_all_called=False) as mock:
        route = site_routes(mock, base, pages)
        async with httpx.AsyncClient() as client:
            await crawl_sitemap(make_fetcher(client), base)
    assert "/sitemap.xml" not in [c.request.url.path for c in route.calls]


async def test_sitemap_not_fetched_when_first_legal_fallback_found() -> None:
    base = "https://fallback-da-example.de"
    pages = {"/": httpx.Response(200, headers=HTML, text="<html></html>"),
             "/impressum": httpx.Response(200, headers=HTML, text="<html>Impressum</html>")}
    with respx.mock(assert_all_called=False) as mock:
        route = site_routes(mock, base, pages)
        async with httpx.AsyncClient() as client:
            await crawl_sitemap(make_fetcher(client), base)
    assert "/sitemap.xml" not in [c.request.url.path for c in route.calls]


async def test_sitemap_disallowed_by_robots_not_fetched() -> None:
    base = "https://kein-sitemap-example.de"
    pages = {"/": httpx.Response(200, headers=HTML, text="<html></html>"),
             "/sitemap.xml": httpx.Response(200, headers=XML, text=urlset(base, "/impressum-x"))}
    with respx.mock(assert_all_called=False) as mock:
        route = site_routes(mock, base, pages, robots="User-agent: *\nDisallow: /sitemap.xml\n")
        async with httpx.AsyncClient() as client:
            fetcher = make_fetcher(client)
            await crawl_sitemap(fetcher, base)
    assert "/sitemap.xml" not in [c.request.url.path for c in route.calls]
    assert fetcher.pages["kein-sitemap-example.de"] <= 5
