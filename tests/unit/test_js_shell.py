"""JavaScript app-shell detection."""

from pathlib import Path

import pytest

from leadscraper.crawler.js_shell import is_js_shell

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
GOLDEN = sorted(p.name for p in PAGES.glob("*.html") if p.name != "spa_shell.html")
EXAMPLE_COM = ("<!doctype html><html><head><title>Example Domain</title><meta charset=\"utf-8\">"
               "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>"
               "<div><h1>Example Domain</h1><p>This domain is for use in illustrative examples in "
               "documents. You may use this domain in literature without prior coordination or asking "
               "for permission.</p><p><a href=\"https://www.iana.org/domains/example\">More "
               "information...</a></p></div></body></html>")


def test_spa_fixture_detected() -> None:
    """The reference's ``spa_shell.html`` (ported): app-shell marker, JS noscript banner, no text."""
    assert is_js_shell((PAGES / "spa_shell.html").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", GOLDEN)
def test_golden_pages_not_detected(name: str) -> None:
    assert not is_js_shell((PAGES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("html", [
    EXAMPLE_COM,
    "<html><body><p>Hello</p></body></html>",
    "<html><head><script type=\"application/ld+json\">{}</script></head><body><p>Kurz</p></body></html>",
], ids=["example.com", "tiny", "jsonld-only"])
def test_tiny_static_pages_not_detected(html: str) -> None:
    assert not is_js_shell(html)
