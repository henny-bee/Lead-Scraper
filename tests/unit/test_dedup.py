import pytest

from leadscraper.domain.models import CompanyCandidate
from leadscraper.schemas.scrape import ScrapeRequest
from leadscraper.services.dedup import (
    Deduplicator,
    legal_form_tokens,
    normalize_name,
    registered_domain,
)
from leadscraper.services.planner import build_plan
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.settings import load_settings

pytestmark = pytest.mark.anyio


async def plan_for(country: str, regions: list[str], industries: list[str]):
    req = ScrapeRequest.model_validate({"country": country, "regions": regions,
                                        "industries": industries, "information": ["website"]})
    return build_plan(await Resolver(load_settings({})).resolve(req))


def cand(name: str, ref: str, website: str | None = None, postal: str | None = None,
         **hints: str) -> CompanyCandidate:
    return CompanyCandidate(name=name, source="osm", source_ref=ref, website=website,
                            postal_code=postal, hints=dict(hints))


def by_label(plan, region: str, industry: str):
    return next(s for s in plan.ordered() if s.region_label == region and s.industry_label == industry)


@pytest.mark.parametrize("url,domain", [
    ("www.shop.firma-example.de", "firma-example.de"),
    ("firma-example.de/impressum", "firma-example.de"),
    ("https://WWW.Firma-example.DE/kontakt?x=1", "firma-example.de"),
    ("http://shop.example.co.uk", "example.co.uk"),
    ("https://shop.example.com.au", "example.com.au"),
    ("http://192.168.0.1/", None),
    ("", None),
    (None, None),
    ("http://[::1", None),
])
def test_registered_domain(url, domain) -> None:
    assert registered_domain(url) == domain


def test_normalize_name_strips_accents_and_legal_forms() -> None:
    de = legal_form_tokens("DE")
    assert normalize_name("Übungs Maschinenbau GmbH & Co. KG", de) == "ubungs maschinenbau"
    assert normalize_name("ÜBUNGS-Maschinenbau GmbH", de) == "ubungs maschinenbau"
    gb = legal_form_tokens("GB")
    assert normalize_name("Sample Logistics Ltd", gb) == "sample logistics"


async def test_domain_dedup_www_and_path() -> None:
    plan = await plan_for("Germany", ["Bayern"], ["Maschinenbau"])
    d = Deduplicator(plan)
    s = plan.ordered()[0]
    first = d.add(cand("Firma GmbH", "node/1", "www.shop.firma-example.de"), s)
    second = d.add(cand("Firma Shop", "node/2", "firma-example.de/impressum"), s)
    assert first.new and not second.new and second.entry is first.entry
    assert len(d.entries) == 1 and first.entry.domain == "firma-example.de"


async def test_produktion_maschinenbau_overlap_assigns_most_specific() -> None:
    plan = await plan_for("Germany", ["Bayern"], ["Produktion", "Maschinenbau"])
    d = Deduplicator(plan)
    produktion = by_label(plan, "Bayern", "Produktion")
    maschinenbau = by_label(plan, "Bayern", "Maschinenbau")
    d.add(cand("Muster Maschinenbau GmbH", "way/9", "https://muster-mb-example.de"), produktion)
    res = d.add(cand("Muster Maschinenbau GmbH", "way/9", "https://muster-mb-example.de"), maschinenbau)
    assert res.reassigned and len(d.entries) == 1
    assert d.entries[0].industry_label == "Maschinenbau"
    # reverse order: Maschinenbau first stays Maschinenbau
    d2 = Deduplicator(plan)
    d2.add(cand("Muster", "way/9", "https://muster-mb-example.de"), maschinenbau)
    assert not d2.add(cand("Muster", "way/9", "https://muster-mb-example.de"), produktion).reassigned
    assert d2.entries[0].industry_label == "Maschinenbau"


async def test_equal_specificity_first_slice_wins() -> None:
    plan = await plan_for("Germany", ["Bayern"], ["Großhandel", "Maschinenbau"])
    d = Deduplicator(plan)
    d.add(cand("X", "node/1", "x-example.de"), by_label(plan, "Bayern", "Großhandel"))
    d.add(cand("X", "node/1", "x-example.de"), by_label(plan, "Bayern", "Maschinenbau"))
    assert d.entries[0].industry_label == "Großhandel"


async def test_smaller_area_wins() -> None:
    plan = await plan_for("France", ["Auvergne-Rhône-Alpes", "Ain"], ["Transport routier"])
    d = Deduplicator(plan)
    d.add(cand("Transports Fictif", "node/1", "fictif-exemple.fr"),
          by_label(plan, "Auvergne-Rhône-Alpes", "Transport routier"))
    d.add(cand("Transports Fictif", "node/1", "fictif-exemple.fr"), by_label(plan, "Ain", "Transport routier"))
    assert d.entries[0].region_label == "Ain"


async def test_name_postal_dedup_threshold() -> None:
    plan = await plan_for("Germany", ["Bayern"], ["Maschinenbau"])
    s = plan.ordered()[0]
    d = Deduplicator(plan)
    d.add(cand("Übungs Maschinenbau GmbH", "node/1", postal="80331"), s)
    # same company, different legal-form spelling / word order -> merged
    assert not d.add(cand("Maschinenbau Übungs GmbH & Co. KG", "node/2", postal="80331"), s).new
    # same name, other postal code -> different company
    assert d.add(cand("Übungs Maschinenbau GmbH", "node/3", postal="10115"), s).new
    # similar but below 92 -> different company
    assert d.add(cand("Überall Anlagenbau GmbH", "node/4", postal="80331"), s).new
    # no postal code -> never merged by name (conservative)
    assert d.add(cand("Übungs Maschinenbau GmbH", "node/5"), s).new
    assert len(d.entries) == 4


async def test_threshold_boundary_is_configurable() -> None:
    plan = await plan_for("Germany", ["Bayern"], ["Maschinenbau"])
    s = plan.ordered()[0]
    strict = Deduplicator(plan, threshold=100)
    strict.add(cand("Uebungs Maschinenbau", "node/1", postal="80331"), s)
    assert strict.add(cand("Ubungs Maschinenbau", "node/2", postal="80331"), s).new
    loose = Deduplicator(plan, threshold=90)
    loose.add(cand("Uebungs Maschinenbau", "node/1", postal="80331"), s)
    assert not loose.add(cand("Ubungs Maschinenbau", "node/2", postal="80331"), s).new


async def test_different_domains_never_name_merged_and_merge_fills_fields() -> None:
    plan = await plan_for("Germany", ["Bayern"], ["Maschinenbau"])
    s = plan.ordered()[0]
    d = Deduplicator(plan)
    d.add(cand("Alpha GmbH", "node/1", "alpha-a-example.de", "80331"), s)
    assert d.add(cand("Alpha GmbH", "node/2", "alpha-b-example.de", "80331"), s).new
    d.add(cand("Beta GmbH", "node/3", None, "80331"), s)
    res = d.add(cand("Beta GmbH", "node/4", "beta-example.de", "80331", email="info@beta-example.de"), s)
    assert not res.new and res.entry.candidate.website == "beta-example.de"
    assert res.entry.domain == "beta-example.de" and res.entry.candidate.hints["email"] == "info@beta-example.de"
    assert d.add(cand("Beta Shop", "node/5", "https://www.beta-example.de"), s).entry is res.entry
