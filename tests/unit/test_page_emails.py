"""Extra email pass: inline text, look-alikes, spaced form, microdata, filters."""

import json
import socket
import time
from pathlib import Path

import pytest

from leadscraper.extractors.emails import extract_emails
from leadscraper.extractors.page_emails import (
    extract_page_emails,
    is_placeholder,
    is_placeholder_domain,
    spaced_deobfuscate,
)
from leadscraper.extractors.text import inline_text

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
GOLDEN = sorted(PAGES.glob("*.expected.json"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def guard(*_a, **_k):
        raise AssertionError("network access during extraction")
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


def page(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def test_reference_obfuscated_fixture() -> None:
    found = extract_page_emails(page("ref_obfuscated.html"))
    for kept in ("john@", "info@", "press@", "sales@", "hr@", "support@"):
        assert f"{kept}acme-example.com" in found, kept          # sales@ = full-width, hr@ = spaced
    for dropped in ("logo@2x.png", "user@example.com", "foo@mail.example.com",
                    "3f2a9c81b4d05e67a1b2c3d4@logs.acme-example.com"):
        assert dropped not in found, dropped
    assert found == {f"{x}@acme-example.com" for x in ("hr", "info", "john", "press", "sales", "support")}


def test_reference_cloudflare_fixture() -> None:
    found = extract_page_emails(page("ref_cloudflare.html"))
    assert found == {"dave@acme-example.com", "eve@acme-example.com"}
    assert not any("protected" in e for e in found)


@pytest.mark.parametrize("expected_file", GOLDEN, ids=lambda p: p.name)
def test_golden_pages_unchanged_or_gain_accepted(expected_file: Path) -> None:
    """The pass keeps every address of the goldens; the ref_* goldens record both."""
    html = page(expected_file.name.replace(".expected.json", ".html"))
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    assert sorted(extract_page_emails(html)) == expected.get("page_emails", expected["emails"])


def test_split_inline_tags() -> None:
    assert extract_page_emails("<p>E-Mail: <b>info</b>@firma-example.de</p>") == {"info@firma-example.de"}
    assert extract_page_emails("<p>info<span>@</span>firma-example.de</p>") == {"info@firma-example.de"}
    assert extract_page_emails("<p><span>kontakt</span><span>@firma-example.de</span></p>") == {
        "kontakt@firma-example.de"}
    assert extract_emails("<p>E-Mail: <b>info</b>@firma-example.de</p>") == set()   # alone misses it


def test_inline_text_blocks_and_skips() -> None:
    text = inline_text("<div>a<br>b</div><p>c <i>d</i>e</p><script>x@y-example.de</script><!-- z -->")
    assert text == "a\nb\nc d e"                  # word chars across a node boundary get a space


def test_mixed_spaced_form() -> None:
    assert "john@acme-example.com" in extract_page_emails("<p>john@acme-example dot com</p>")
    prose = "we met at the office dot party"
    assert spaced_deobfuscate(prose) == prose
    assert extract_page_emails(f"<p>{prose}</p>") == set()


def test_lookalikes() -> None:
    assert extract_page_emails("<p>ops﹫acme-example。com</p>") == {"ops@acme-example.com"}
    assert extract_page_emails("<p>a․b@acme-example.com</p>") == {"a.b@acme-example.com"}


@pytest.mark.parametrize("email", [
    "max.mustermann@firma-example.de", "mustermann@firma-example.de", "maxmustermann@firma-example.de",
    "erika.mustermann@firma-example.de", "vorname.nachname@firma-example.de", "ihre.email@firma-example.de",
    "ihre-email@firma-example.de", "ihrname@firma-example.de", "votre.email@societe-example.fr",
    "tu.email@empresa-example.es", "nombre@empresa-example.es", "youremail@firma-example.de",
    "noreply@firma-example.de", "test-user@firma-example.de", "info@musterfirma.de",
    "kontakt@mustermann.de", "john@yourcompany.com", "x@localhost"])
def test_german_placeholders_rejected(email: str) -> None:
    assert is_placeholder(email)
    assert extract_page_emails(f"<p>{email}</p>") == set()


@pytest.mark.parametrize("email", [
    "firstnamelastname@firma-example.de", "mustermann-bau@firma-example.de", "tester@firma-example.de",
    "testimonials@firma-example.de", "barbara@firma-example.de", "planung@firma-example.de"])
def test_real_addresses_containing_placeholder_words_survive(email: str) -> None:
    assert not is_placeholder(email)
    assert extract_page_emails(f"<p>{email}</p>") == {email}


def test_generic_role_locals_on_company_domain_survive() -> None:
    """Generic role locals and email/name/user/office are never placeholders on their own."""
    for email in ("mail@firma-example.de", "email@firma-example.de", "post@firma-example.de",
                  "office@firma-example.de", "user@firma-example.de", "name@firma-example.de",
                  "info@firma-example.de", "kontakt@firma-example.de"):
        assert not is_placeholder(email), email
        assert extract_page_emails(f"<p>{email}</p>") == {email}
    assert is_placeholder("email@example.com")
    assert extract_page_emails("<p>email@example.com</p>") == set()


def test_example_substring_domain_survives() -> None:
    assert not is_placeholder_domain("firma-example.de") and not is_placeholder_domain("example-widgets.com")
    assert is_placeholder_domain("example.de") and is_placeholder_domain("example.co.uk")
    assert is_placeholder_domain("mail.example.com")
    assert extract_page_emails("<p>info@firma-example.de</p>") == {"info@firma-example.de"}


def test_hex_long_and_asset_filters() -> None:
    assert extract_page_emails("<p>a1b2c3d4e5f60718293a4b5c@firma-example.de</p>") == set()
    assert extract_page_emails(f"<p>{'x' * 65}@firma-example.de</p>") == set()
    assert extract_page_emails('<link href="favicon@2x.ico">') == set()


def test_microdata_email() -> None:
    html = ('<div itemscope itemtype="https://schema.org/Organization">'
            '<meta itemprop="email" content="mailto:info@mikro-example.de">'
            '<span itemprop="email">vertrieb [at] mikro-example [punkt] de</span></div>')
    assert extract_page_emails(html) == {"info@mikro-example.de", "vertrieb@mikro-example.de"}


def test_large_page_fast() -> None:
    long_tokens = ("1" * 50_000 + " " + "a" * 50_000 + "@ " + "12.34." * 8000 + " ") * 5
    html = (f"<html><body><p>{long_tokens}</p><p>{'Text ' * 300_000}</p>"
            "<p>mail hello@schnell-example.de</p></body></html>")
    assert len(html) > 2_000_000
    started = time.perf_counter()
    found = extract_page_emails(html)
    assert time.perf_counter() - started < 1.0
    assert found == {"hello@schnell-example.de"}


def test_inline_join_does_not_glue_words() -> None:
    """Review fix 1: adjacent word characters from different nodes are not glued."""
    cases = [
        ("<p><span>Mail</span><span>info@firma-example.de</span></p>", {"info@firma-example.de"}),
        ('<p><span>E-Mail</span><a href="mailto:info@firma-example.de">info@firma-example.de</a></p>',
         {"info@firma-example.de"}),
        ('<p><span>Ansprechpartner</span><a href="mailto:max.meier@firma-example.de">'
         'max.meier@firma-example.de</a></p>', {"max.meier@firma-example.de"}),
    ]
    for html, expected in cases:
        assert extract_page_emails(html) == expected, html
    assert inline_text("<p><span>Mail</span><span>info@x-example.de</span></p>") == "Mail info@x-example.de"
    assert inline_text("<p><b>info</b>@x-example.de</p>") == "info@x-example.de"


def test_raw_pass_hints_keep_entities_and_encoded_forms() -> None:
    assert extract_page_emails("<p>info&#64;firma-example.de</p>") == {"info@firma-example.de"}
    assert extract_page_emails("<p>info&commat;firma-example.de</p>") == {"info@firma-example.de"}
    assert extract_page_emails('<a href="mailto:%69nfo%40firma-example.de">x</a>') == {"info@firma-example.de"}
