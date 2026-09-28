from pathlib import Path

from leadscraper.extractors.jsonld import extract_organizations, is_org_type

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"


def script(body: str) -> str:
    return f'<html><head><script type="application/ld+json">{body}</script></head></html>'


def test_german_impressum_organization() -> None:
    (org,) = extract_organizations((PAGES / "de_impressum.html").read_text(encoding="utf-8"))
    assert org.types == ("Organization",)
    assert org.legal_name == "Muster Maschinenbau GmbH" and org.name == "Muster Maschinenbau"
    assert org.emails == ["info@muster-maschinenbau-example.de"]           # "mailto:" stripped
    assert org.telephones == ["+49 89 1234567"]
    assert (org.street, org.postal_code, org.locality, org.country) == (
        "Industriestraße 12", "80331", "München", "DE")
    assert org.address_text == "Industriestraße 12, 80331 München, DE"


def test_graph_type_list_and_contact_point() -> None:
    orgs = extract_organizations((PAGES / "en_contact.html").read_text(encoding="utf-8"))
    assert len(orgs) == 1
    org = orgs[0]
    assert org.types == ("Organization", "Corporation") and org.name == "Sample Logistics Ltd"
    assert org.emails == ["support@sample-logistics-example.co.uk"] and org.telephones == ["+44-20-7946-0958"]


def test_top_level_list_nested_publisher_and_local_business() -> None:
    html = script('[{"@type":"WebPage","publisher":{"@type":"Organization","name":"Pub GmbH",'
                  '"email":["a@pub-example.de","A@pub-example.de"]}},'
                  '{"@type":"AutoRepair","name":"Werkstatt X","telephone":"089 1",'
                  '"address":"Hauptstr. 1, 80331 München","vatID":"DE136695976"}]')
    pub, shop = extract_organizations(html)
    assert pub.name == "Pub GmbH" and pub.emails == ["a@pub-example.de"]
    assert shop.types == ("AutoRepair",) and shop.address_text == "Hauptstr. 1, 80331 München"
    assert shop.vat_id == "DE136695976"


def test_multiple_blocks_invalid_json_and_wrappers() -> None:
    html = ('<script type="application/ld+json">{not json}</script>'
            '<script type="application/ld+json"><!-- {"@type":"LocalBusiness","name":"A"} --></script>'
            '<script type="application/ld+json">{"@type":"Product","name":"Widget"}</script>'
            '<script type="application/ld+json">{"@type":"schema:Organization","name":"B\nC"}</script>')
    names = [o.name for o in extract_organizations(html)]
    assert names == ["A", "B\nC"]


def test_type_predicate() -> None:
    assert is_org_type(("Organization",)) and is_org_type(("TravelAgency", "LocalBusiness"))
    assert is_org_type(("SportsOrganization",)) and is_org_type(("HomeAndConstructionBusiness",))
    assert not is_org_type(("Product",)) and not is_org_type(())


def test_no_jsonld() -> None:
    assert extract_organizations("<html><body>nothing</body></html>") == []
