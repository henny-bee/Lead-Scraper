from pathlib import Path

import pytest

from leadscraper.extractors import address, legal, objection, phone
from leadscraper.extractors.text import page_text
from leadscraper.services.resolver.profile import ProfileBuilder

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"
PROFILES = ProfileBuilder()


def text_of(name: str) -> str:
    return page_text((PAGES / name).read_text(encoding="utf-8"))


DE_TEXT = text_of("de_impressum.html")

GB_CONTACT_TEXT = """About Us
Sample Logistics Ltd
221 Baker Street
London NW1 6XE
Tel. 020 7946 0958
VAT: GB 980 7806 84"""


def test_page_text_drops_scripts_and_splits_lines() -> None:
    assert "Sentry" not in DE_TEXT and "@context" not in DE_TEXT
    assert "Muster Maschinenbau GmbH" in DE_TEXT.splitlines()


# --- German Impressum fixture (acceptance) ----------------------------------------------------------
def test_de_legal_name_and_form() -> None:
    hit = legal.extract_legal_name([DE_TEXT], "DE", source_name="Muster Maschinenbau")
    assert hit == legal.LegalName("Muster Maschinenbau GmbH", "GmbH")


def test_de_register_number_validated() -> None:
    assert legal.extract_register_number(DE_TEXT, "DE") == "München HRB 123456"


def test_de_vat_validated_by_stdnum() -> None:
    assert legal.extract_vat_id(DE_TEXT, "DE") == "DE136695976"
    assert legal.extract_vat_id("USt-IdNr.: DE 123456789", "DE") is None      # checksum fails


def test_de_phone_e164_and_fax_skipped() -> None:
    assert phone.extract_phone([DE_TEXT], "DE") == "+49891234567"
    assert phone.extract_phone(["Fax: 089 1234568\nTel. 089 7654321"], "DE") == "+49897654321"
    assert phone.extract_phone([], "DE", structured=["+49 89 1234567"]) == "+49891234567"
    assert phone.extract_phone(["keine Nummer"], "DE") is None


def test_de_postcode_and_address() -> None:
    res = address.extract_address(DE_TEXT, PROFILES.get("DE").postal_patterns, "DE")
    assert res == address.AddressResult("Industriestraße 12, 80331 München", "80331")


def test_de_objection_sentence() -> None:
    assert objection.has_marketing_objection([DE_TEXT])
    classic = ("Der Nutzung von im Rahmen der Impressumspflicht veröffentlichten Kontaktdaten zur "
               "Übersendung von nicht ausdrücklich angeforderter Werbung und "
               "Informationsmaterialien wird hiermit widersprochen.")
    assert objection.has_marketing_objection([classic])


# --- English (GB) contact page (acceptance) ---
def test_gb_suffix_form_ltd() -> None:
    hit = legal.extract_legal_name([GB_CONTACT_TEXT], "GB")
    assert hit == legal.LegalName("Sample Logistics Ltd", "Ltd")
    assert legal.extract_legal_name([], "GB", candidates=["Sample Logistics Ltd"]).legal_form == "Ltd"


def test_gb_other_fields() -> None:
    assert phone.extract_phone([GB_CONTACT_TEXT], "GB") == "+442079460958"
    res = address.extract_address(GB_CONTACT_TEXT, PROFILES.get("GB").postal_patterns, "GB")
    assert res == address.AddressResult("221 Baker Street, London NW1 6XE", "NW1 6XE")
    assert legal.extract_vat_id(GB_CONTACT_TEXT, "GB") == "980780684"
    assert not objection.has_marketing_objection([GB_CONTACT_TEXT])


def test_prefix_legal_forms_are_data_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    # generic prefix-form branch, exercised with a synthetic country entry
    monkeypatch.setattr(legal, "_forms_file",
                        lambda *a, **k: {"XX": {"prefix": ["Pfx"], "suffix": ["Holding"]}})
    legal.legal_forms.cache_clear()
    legal._name_regexes.cache_clear()
    try:
        assert legal.parse_legal_name("Pfx Nova Works Holding", "XX") == legal.LegalName(
            "Pfx Nova Works Holding", "Pfx")
        assert legal.parse_legal_name("Pfx Nova Works", "XX") == legal.LegalName("Pfx Nova Works", "Pfx")
        assert legal.extract_legal_name([], "XX", candidates=["Pfx Nova Works"]).legal_form == "Pfx"
    finally:
        legal.legal_forms.cache_clear()
        legal._name_regexes.cache_clear()


# --- other countries / edge cases -------------------------------------------------------------------
def test_fr_legal_siren_vat() -> None:
    fr = text_of("fr_mentions.html")
    assert legal.extract_legal_name([fr], "FR") == legal.LegalName("Transports Exemple SAS", "SAS")
    assert legal.extract_register_number(fr, "FR") == "732829320"
    assert legal.extract_register_number("RCS Paris 123 456 789", "FR") is None     # bad Luhn
    assert legal.extract_vat_id("TVA intracommunautaire : FR 40 303 265 045", "FR") == "FR40303265045"


@pytest.mark.parametrize("line,cc,expected", [
    ("Übungs Logistik GmbH & Co. KG", "DE", ("Übungs Logistik GmbH & Co. KG", "GmbH & Co. KG")),
    ("Firma: Muster Handels GMBH", "DE", ("Muster Handels GMBH", "GmbH")),
    ("Example Holdings Ltd", "GB", ("Example Holdings Ltd", "Ltd")),
    ("Jansen Transport B.V.", "NL", ("Jansen Transport B.V.", "B.V.")),
    ("Transportes García S.L.", "ES", ("Transportes García S.L.", "S.L.")),
])
def test_suffix_forms(line: str, cc: str, expected: tuple[str, str]) -> None:
    assert legal.parse_legal_name(line, cc) == legal.LegalName(*expected)


@pytest.mark.parametrize("line", [
    "Angaben gemäß § 5 TMG", "Umsatzsteuer-Identifikationsnummer gemäß § 27 a UStG: DE136695976",
    "Wir liefern in ganz Deutschland und Österreich und der Schweiz seit vielen vielen Jahren "
    "Qualität an unsere Kunden die auf uns bauen können GmbH",
])
def test_no_false_legal_names(line: str) -> None:
    assert legal.parse_legal_name(line, "DE") is None


def test_register_raw_when_court_unknown_and_unsupported_country() -> None:
    assert legal.extract_register_number("Registergericht: Xyzstadt, HRB 98765", "DE") == "HRB 98765"
    assert legal.extract_register_number("Handelsregister HRB 4711", "DE") == "HRB 4711"
    assert legal.extract_register_number("Company number 01234567", "GB") is None


def test_source_name_preferred_among_candidates() -> None:
    text = "Mitglied der Beispiel Verband AG\nMuster Maschinenbau GmbH"
    hit = legal.extract_legal_name([text], "DE", source_name="Muster Maschinenbau")
    assert hit.legal_name == "Muster Maschinenbau GmbH"
    assert legal.extract_legal_name([text], "DE").legal_name == "Mitglied der Beispiel Verband AG"


def test_postcode_needs_city_and_skips_phone_lines() -> None:
    pats = PROFILES.get("DE").postal_patterns
    assert address.extract_address("Tel. 089 12345 67\nPostfach 80331", pats, "DE").postal_code == "80331"
    assert address.extract_address("Tel. +49 89 12345", pats, "DE") is None
    assert address.extract_address("Kundennummer 12345", pats, "DE") is None
    assert address.extract_address("HRB 12345 Amtsgericht", pats, "DE") is None
    assert address.extract_address("12345", pats, "DE") is None


def test_geonames_validation_when_dataset_present(tmp_path: Path) -> None:
    (tmp_path / "DE.txt").write_text("DE\t80331\tMünchen\nDE\t10115\tBerlin\n", encoding="utf-8")
    known = address.geonames_postal_codes("DE", str(tmp_path))
    pats = PROFILES.get("DE").postal_patterns
    assert address.extract_address("99999 Nirgendwo", pats, "DE", known_codes=known) is None
    assert address.extract_address("10115 Berlin", pats, "DE", known_codes=known).postal_code == "10115"
    assert address.geonames_postal_codes("DE") is None            # v0.3 ships no GeoNames data


@pytest.mark.parametrize("text,expected", [
    ("We object to the use of our contact details published here for advertising purposes.", True),
    ("Please do not send unsolicited marketing emails.", True),
    ("Toute prospection commerciale par e-mail est interdite.", True),
    ("No aceptamos publicidad no solicitada.", True),
    ("Vietata la pubblicità non richiesta.", True),
    ("Werbung für unsere Produkte finden Sie im Katalog.", False),
    ("Newsletter abonnieren – Werbung ist uns wichtig.", False),
])
def test_objection_patterns(text: str, expected: bool) -> None:
    assert objection.has_marketing_objection([text]) is expected
