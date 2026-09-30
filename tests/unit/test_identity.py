"""Company-identity check for looked-up websites."""

from leadscraper.extractors.identity import matches_company, translit
from leadscraper.services.dedup import legal_form_tokens

FORMS = legal_form_tokens("DE")
GENERIC = ["logistik", "spedition", "lager", "transport"]


def match(texts, name, postal_code="28195", city="Bremen", titles=()):
    return matches_company(texts, name=name, postal_code=postal_code, city=city, area_name="Bremen",
                           country_code="DE", legal_form_tokens=FORMS, generic_tokens=GENERIC,
                           titles=titles)


def test_umlaut_transliteration() -> None:
    assert translit("Förde Straße Müller") == "foerde strasse mueller"
    assert match(["Spedition Foerde GmbH", "28195 Bremen"], "Spedition Förde").ok
    assert match(["Spedition Förde GmbH", "28195 Bremen"], "Spedition Foerde").ok


def test_legal_form_stripping() -> None:
    r = match(["Weserbogen Handel", "Hafenstr. 1, 28195 Bremen"], "Weserbogen Handel GmbH & Co. KG")
    assert r.ok and r.name_score == 100 and r.location_hit


def test_postcode_token_boundaries() -> None:
    assert not match(["Weserbogen Handel GmbH", "128195 Bremenhaven"], "Weserbogen Handel", city=None).ok
    assert match(["Weserbogen Handel GmbH", "D-28195 Bremen"], "Weserbogen Handel", city=None).ok
    r = match(["Weserbogen Handel GmbH", "1281950 Irgendwo"], "Weserbogen Handel", city=None)
    assert not r.location_hit and not r.ok and "location" in r.reasons


def test_city_evidence_when_postcode_missing_on_page() -> None:
    assert match(["Weserbogen Handel GmbH", "Hafenstraße 1, Bremen"], "Weserbogen Handel").ok


def test_name_only_strict_rule() -> None:
    texts = ["Findorff Express GmbH", "Kontakt"]
    assert match(texts, "Findorff Express", postal_code=None, city=None).ok
    weaker = match(["Findorff Expressdienste Nord GmbH"], "Findorff Express", postal_code=None, city=None)
    assert weaker.name_score < 95 and not weaker.ok and "name_only" in weaker.reasons


def test_title_counts_as_name_evidence() -> None:
    assert match(["Impressum", "28195 Bremen"], "Kranich Transporte", titles=["Kranich Transporte | Start"]).ok


def test_other_company_rejected() -> None:
    r = match(["Möwenflug Kurierdienst GmbH", "Isarweg 1", "80331 München"], "Spedition Möwe Example")
    assert not r.ok


def test_empty_texts() -> None:
    r = match([], "Weserbogen Handel")
    assert not r.ok and r.name_score == 0 and not r.location_hit


def test_generic_name_tokens_alone_not_enough() -> None:
    """"Logistik Bremen GmbH" (city Bremen, industry keyword Logistik) must not match the Impressum
    of "Nord Logistik Bremen GmbH, 28195 Bremen" although postcode and city match."""
    r = match(["Nord Logistik Bremen GmbH", "Am Hafen 3", "28195 Bremen"], "Logistik Bremen GmbH")
    assert r.location_hit and r.ok is False and r.reasons == ("generic_name",)
    same = match(["Logistik Bremen GmbH", "Am Hafen 3", "28195 Bremen"], "Logistik Bremen GmbH")
    assert same.ok                                    # the exact legal name + location is enough


def test_other_company_sharing_surname_rejected() -> None:
    """"Spedition Müller" must not match "Müller Bau GmbH" in the same town."""
    r = match(["Müller Bau GmbH", "Hafenstr. 2", "28195 Bremen"], "Spedition Müller GmbH")
    assert r.location_hit and not r.ok and r.name_score < 85
    assert match(["Spedition Müller GmbH", "Hafenstr. 2", "28195 Bremen"], "Spedition Müller GmbH").ok


def test_brand_title_with_legal_line_accepted() -> None:
    r = match(["Willkommen", "Spedition Müller GmbH & Co. KG", "28195 Bremen"], "Spedition Müller GmbH",
              titles=["Müller Logistik"])
    assert r.ok and r.name_score == 100


# --- review: strict location from the site's own address (search/guess) ------------------------------
def test_own_address_legal_page_then_jsonld() -> None:
    from leadscraper.extractors.address import own_address
    from leadscraper.extractors.jsonld import JsonLdOrg
    from leadscraper.services.resolver.profile import ProfileBuilder
    pats = ProfileBuilder().get("DE").postal_patterns
    legal = ["Impressum\nKranich Transporte GmbH\nLeopoldstr. 5\n80331 München\nRegistergericht: Amtsgericht Bremen"]
    assert own_address(legal, [], pats, "DE") == ("80331", "München")
    org = JsonLdOrg(types=("Organization",), postal_code="28195", locality="Bremen")
    assert own_address(["Impressum ohne Adresse"], [org], pats, "DE") == ("28195", "Bremen")
    assert own_address(legal, [org], pats, "DE") == ("80331", "München")      # legal page first
    assert own_address(["Niederlassungen: Hamburg, Bremen, Köln"], [], pats, "DE") is None


def strict(texts, own, name="Kranich Transporte", postal_code="28195", city="Bremen"):
    return matches_company(texts, name=name, postal_code=postal_code, city=city, area_name="Bremen",
                           country_code="DE", legal_form_tokens=FORMS, generic_tokens=GENERIC,
                           own_address=own, strict_location=True)


def test_strict_location_postcode_mismatch_is_final() -> None:
    texts = ["Kranich Transporte GmbH", "Leopoldstr. 5", "80331 München", "Registergericht: Amtsgericht Bremen"]
    r = strict(texts, ("80331", "München"))
    assert not r.ok and r.reasons == ("postcode_mismatch",)
    lenient = matches_company(texts, name="Kranich Transporte", postal_code="28195", city="Bremen",
                              area_name="Bremen", country_code="DE", legal_form_tokens=FORMS,
                              generic_tokens=GENERIC)
    assert lenient.ok                                      # rule: the city anywhere on the page


def test_strict_location_city_and_fallback() -> None:
    texts = ["Kranich Transporte GmbH", "Am Hafen 1", "28195 Bremen"]
    assert strict(texts, ("28195", "Bremen")).ok
    assert strict(texts, (None, "Bremen-Vegesack"), postal_code=None).ok       # city as a whole word
    assert not strict(texts, (None, "Hamburg"), postal_code=None).ok
    assert strict(texts, None).ok                                              # no own address: lenient
