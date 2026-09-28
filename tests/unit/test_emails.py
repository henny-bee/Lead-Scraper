import json
import socket
from pathlib import Path

import pytest

from leadscraper.extractors import emails as emails_mod
from leadscraper.extractors.emails import decode_cfemail, extract_emails

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
GOLDEN = sorted(PAGES.glob("*.expected.json"))


def cf(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Extraction must never touch the network (tldextract uses its bundled PSL snapshot)."""
    def guard(*_a, **_k):
        raise AssertionError("network access during extraction")
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.mark.parametrize("html,expected", [
    ('<a href="mailto:Info@Firma-example.de?subject=Hallo">Mail</a>', {"info@firma-example.de"}),
    ('<a href="mailto:a@firma-example.de,b@firma-example.de">x</a>', {"a@firma-example.de", "b@firma-example.de"}),
    ("info&#64;firma-example.de", {"info@firma-example.de"}),
    ("kontakt&#x40;firma-example&#46;de", {"kontakt@firma-example.de"}),
    ("info [at] firma-example [punkt] de", {"info@firma-example.de"}),
    ("info (at) firma-example (dot) de", {"info@firma-example.de"}),
    ("info {at} firma-example {dot} de", {"info@firma-example.de"}),
    ("info at firma-example dot de", {"info@firma-example.de"}),
    ("ventas (arroba) empresa-ejemplo (punto) es", {"ventas@empresa-ejemplo.es"}),
    ("contact [arobase] societe-exemple [point] fr", {"contact@societe-exemple.fr"}),
    ("info [chiocciola] azienda-esempio [punto] it", {"info@azienda-esempio.it"}),
    ("info [at] firma-example [dot] co [dot] uk", {"info@firma-example.co.uk"}),
    ("info [@] firma-example [.] de", {"info@firma-example.de"}),
    ("Schreiben Sie an Vertrieb@Firma-example.DE.", {"vertrieb@firma-example.de"}),
])
def test_positive_cases(html: str, expected: set[str]) -> None:
    assert extract_emails(html) == expected


def test_percent_encoded_mailto() -> None:
    """Q-E9 fix: EMAIL_RE no longer matches the raw text after '%69'; only the unquoted mailto."""
    assert extract_emails('<a href="mailto:%69nfo@firma-example.de">x</a>') == {"info@firma-example.de"}
    assert extract_emails("Mail: info@firma-example.de") == {"info@firma-example.de"}


def test_cloudflare_attribute_and_link() -> None:
    html = (f'<a class="__cf_email__" data-cfemail="{cf("info@firma-example.de")}">[email protected]</a>'
            f'<a href="/cdn-cgi/l/email-protection#{cf("kontakt@firma-example.de", 0x1F)}">x</a>')
    assert extract_emails(html) == {"info@firma-example.de", "kontakt@firma-example.de"}
    assert decode_cfemail(cf("a@b-example.de", 0x7A)) == "a@b-example.de"


@pytest.mark.parametrize("html", [
    '<img src="logo@2x.png"><img srcset="icon@3x.webp 3x, bg@2x.jpg">',
    'Sentry.init({dsn:"https://abc123def456@o123456.ingest.sentry.io/789"})',
    '<script src="https://browser.sentry-cdn.com/x.js" data-dsn="https://k@sentry.io/1"></script>',
    "Wir sind at home. Punkt. Treffen Sie uns at the fair in Hall 5 punkt 3.",
    "Punkt 1: Haftung. Stand at 2024 punkt",
    "max@example.com name@beispiel.de test@domain-example.de",
    "5f3c1b2a9d@sentry-next.wixpress.com",
    "@media (min-width: 600px) { a { color: red } }",
    "styles@1.css app@2.js",
])
def test_false_positives_rejected(html: str) -> None:
    assert extract_emails(html) == set()


@pytest.mark.parametrize("expected_file", GOLDEN, ids=lambda p: p.name)
def test_golden_pages(expected_file: Path) -> None:
    html = expected_file.with_name(expected_file.name.replace(".expected.json", ".html"))
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    assert sorted(extract_emails(html.read_text(encoding="utf-8"))) == expected["emails"]


def test_golden_set_covers_languages() -> None:
    assert {p.name.split("_")[0] for p in GOLDEN} >= {"de", "fr", "en", "es"}


def test_tldextract_is_offline() -> None:
    assert emails_mod._TLD.suffix_list_urls == ()
    assert emails_mod._TLD("o1.ingest.sentry.io").top_domain_under_public_suffix == "sentry.io"


def test_module_is_architecture_block_with_q_e9_fix() -> None:
    root = Path(__file__).resolve().parents[2]
    arch = (root / "ARCHITECTURE.md").read_text(encoding="utf-8")
    start = arch.index("``` python\n# src/leadscraper/extractors/emails.py")
    block = arch[start + len("``` python\n"):arch.index("```", start + 10)]
    # Exactly one approved deviation (PLAN Q-E9): "%" added to the EMAIL_RE lookbehind + comment.
    old = r'EMAIL_RE = re.compile(r"(?<![\w.+-])'
    new = r'EMAIL_RE = re.compile(r"(?<![\w.+%-])'
    assert block.count(old) == 1
    line_end = block.index("\n", block.index(old))
    expected = (block[:line_end].replace(old, new) + '  # PLAN Q-E9: "%" in lookbehind'
                + block[line_end:])
    actual = (root / "src" / "leadscraper" / "extractors" / "emails.py").read_text(encoding="utf-8")
    assert actual == expected
