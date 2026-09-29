"""Shared pytest configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote

import httpx
import pytest
import respx

SEARCH_HOSTS = frozenset({"html.duckduckgo.com", "duckduckgo.com", "searxng.test", "bing.com",
                          "www.bing.com"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _fresh_log_stream() -> None:
    """Structlog's PrintLogger keeps the stream it was configured with; a test that configures
    logging under ``capsys`` (``test_settings:test_logging_configures``) leaves a closed stream
    behind, which breaks the next test that logs."""
    from leadscraper.observability.logging import configure_logging
    configure_logging("test")


def is_search_host(host: str) -> bool:
    host = host.lower()
    return host in SEARCH_HOSTS or host.startswith(("google.", "www.google."))


def search_guard_active(node) -> bool:
    """The guard's decision: active unless the test is ``live``/``benchmark`` or uses
    ``search_mock``."""
    if node.get_closest_marker("live") is not None or node.get_closest_marker("benchmark") is not None:
        return False
    return "search_mock" not in getattr(node, "fixturenames", ())


@pytest.fixture
def search_guard_decision():
    """Exposes:func:`search_guard_active` to tests."""
    return search_guard_active


@pytest.fixture(autouse=True)
def _search_host_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if not search_guard_active(request.node):
        return
    original_async, original_sync = httpx.AsyncClient.send, httpx.Client.send

    def refuse(req: httpx.Request) -> None:
        if is_search_host(req.url.host):
            raise AssertionError(f"unmocked web search request to {req.url.host} "
                                 "(use the search_mock fixture or WEB_SEARCH_URL=off)")

    # httpx calls ``send(request=..., ...)`` by keyword, so the parameter name must be "request"
    async def guarded_async(self, request, *args, **kwargs):
        refuse(request)
        return await original_async(self, request, *args, **kwargs)

    def guarded_sync(self, request, *args, **kwargs):
        refuse(request)
        return original_sync(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async)
    monkeypatch.setattr(httpx.Client, "send", guarded_sync)


def ddg_page(results: list[tuple[str, str]]) -> str:
    """A DuckDuckGo-HTML-like results page (the markup parsed by ``web_search.parse_ddg_html``)."""
    items = "".join(
        '<div class="result results_links results_links_deep web-result">'
        '<div class="links_main links_deep result__body"><h2 class="result__title">'
        f'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg={quote(url, safe="")}'
        f'&amp;rut=abc">{title}</a></h2><a class="result__snippet" href="#">{title}</a></div></div>'
        for url, title in results)
    return f'<html><body><div id="links" class="results">{items}</div></body></html>'


@dataclass
class SearchMock:
    """Routes the DuckDuckGo/SearXNG hosts (incl. robots.txt) into a respx router and provides a
    ``SearchGate`` with (almost) no spacing."""

    gate: object
    serps: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)
    robots: httpx.Response | None = None

    def serp(self, needle: str, results: list[tuple[str, str]]) -> SearchMock:
        self.serps[needle.lower()] = results
        return self

    def results_for(self, query: str) -> list[tuple[str, str]]:
        low = query.lower()
        return next((r for needle, r in self.serps.items() if needle in low), [])

    def install(self, router: respx.MockRouter) -> SearchMock:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return self.robots or httpx.Response(200, text="User-agent: *\nAllow: /\n")
            query = (parse_qs(request.url.query.decode()).get("q") or [""])[0]
            self.queries.append(query)
            hits = self.results_for(query)
            if request.url.host == "searxng.test":
                return httpx.Response(200, json={"results": [{"url": u, "title": t, "content": t}
                                                             for u, t in hits]})
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                  text=ddg_page(hits))

        router.route(host__in=("html.duckduckgo.com", "searxng.test")).mock(side_effect=handler)
        return self


@pytest.fixture
def search_mock() -> SearchMock:
    from leadscraper.sources.web_search import SearchGate
    return SearchMock(gate=SearchGate(min_interval_s=0.001))
