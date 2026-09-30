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


# --- exact-segment ranking, fallback paths per kind --------------------------------------------------
def test_exact_segment_beats_substring() -> None:
    base = "https://www.firma-example.de/"
    html = ('<a href="/blog/kontaktformular-tipps">Kontaktformular Tipps</a>'
            '<a href="/contact">Contact</a>'
            '<a href="/blog/contact-center-tips">Contact center tips</a>'
            '<a href="/kontakt.html">Hier</a>')
    urls = [u for u, _ in find_contact_links(html, base, DE.contact_keywords)]
    # exact matches first ("kontakt" segment of kontakt.html, "contact" label/segment) in keyword
    # order, then the substring matches in keyword order
    assert urls == ["https://www.firma-example.de/kontakt.html", "https://www.firma-example.de/contact",
                    "https://www.firma-example.de/blog/kontaktformular-tipps",
                    "https://www.firma-example.de/blog/contact-center-tips"]


def test_fallback_paths_for_languages() -> None:
    """Decision: the country's languages in profile order, then en, deduplicated, per kind."""
    builder = ProfileBuilder()
    assert builder.fallback_paths_for(("de",)) == (("/impressum", "/imprint", "/legal-notice"),
                                                   ("/kontakt", "/contact", "/contact-us"))
    assert builder.fallback_paths_for(("en",)) == (("/imprint", "/legal-notice"), ("/contact", "/contact-us"))
    assert builder.fallback_paths_for(("de", "fr", "it")) == (
        ("/impressum", "/mentions-legales", "/note-legali", "/imprint", "/legal-notice"),
        ("/kontakt", "/contact", "/nous-contacter", "/contatti", "/contact-us"))
    assert builder.fallback_paths_for(("pl",)) == builder.fallback_paths_for(("en",))   # no own paths
    assert builder.fallback_paths == ("/impressum", "/mentions-legales", "/aviso-legal", "/contact")


def test_contact_markers_loaded() -> None:
    pages = load_contact_pages()
    assert "kontakt" in pages.contact_markers and "über uns" not in pages.contact_markers
    assert pages.fallback_by_kind["legal"]["fr"] == ("/mentions-legales",)
