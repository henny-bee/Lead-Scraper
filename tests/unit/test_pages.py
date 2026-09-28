from pathlib import Path

from leadscraper.crawler.pages import find_contact_links, html_lang
from leadscraper.services.resolver.profile import ProfileBuilder, load_contact_pages

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
DE = ProfileBuilder().get("DE")
FR = ProfileBuilder().get("FR")


def read(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def test_html_lang() -> None:
    assert html_lang(read("de_home.html")) == "de"
    assert html_lang(read("fr_home.html")) == "fr"
    assert html_lang("<html><body></body></html>") is None


def test_impressum_link_found_on_german_fixture() -> None:
    links = find_contact_links(read("de_home.html"), "https://www.muster-maschinenbau-example.de/",
                               DE.contact_keywords)
    urls = [u for u, _ in links]
    assert urls[0] == "https://www.muster-maschinenbau-example.de/impressum/"      # legal notice first
    assert "https://www.muster-maschinenbau-example.de/ueber-uns/kontakt" in urls
    assert not any("agentur-webdesign" in u for u in urls)               # other domain ignored
    assert not any("datenschutz" in u or "produkte" in u for u in urls)
    assert dict(links)["https://www.muster-maschinenbau-example.de/impressum/"] == "impressum"


def test_french_links_by_text_and_url() -> None:
    links = find_contact_links(read("fr_home.html"), "https://transports-exemple.fr", FR.contact_keywords)
    assert [u for u, _ in links] == ["https://transports-exemple.fr/mentions-legales",
                                     "https://transports-exemple.fr/nous-contacter"]


def test_no_matching_links() -> None:
    assert find_contact_links(read("no_links_home.html"), "https://demo-logistik-example.de", DE.contact_keywords) == []


def test_subdomain_of_same_company_allowed_and_dedup() -> None:
    html = ('<a href="https://kontakt.firma-example.de/Kontakt">Kontakt</a>'
            '<a href="/impressum">Impressum</a><a href="/impressum#x">Impressum</a>')
    links = find_contact_links(html, "https://www.firma-example.de/", DE.contact_keywords)
    assert [u for u, _ in links] == ["https://www.firma-example.de/impressum",
                                     "https://kontakt.firma-example.de/Kontakt"]


def test_legal_markers_loaded() -> None:
    markers = load_contact_pages().legal_markers
    assert "impressum" in markers and "mentions légales" in markers
