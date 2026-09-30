import pytest

from leadscraper.schemas.scrape import ScrapeRequest
from leadscraper.services.planner import (
    area_specificity,
    build_plan,
    industry_specificity,
    overfetch_target,
    slice_key,
)
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.industry import default_catalog
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.settings import load_settings

pytestmark = pytest.mark.anyio

GERMANY = {"country": "Germany", "regions": ["Bayern", "NRW", "Hessen"],
           "industries": ["Maschinenbau", "Logistik", "Großhandel", "Produktion"],
           "information": ["company_name", "company_email", "website"], "max_output": 1000}


async def resolved(body: dict):
    return await Resolver(load_settings({})).resolve(ScrapeRequest.model_validate(body))


async def test_a35_example_twelve_slices_with_echo_labels() -> None:
    plan = build_plan(await resolved(GERMANY))
    slices = plan.ordered()
    assert len(slices) == 12
    assert {s.region_label for s in slices} == {"Bayern", "NRW", "Hessen"}      # labels echoed
    assert {s.industry_label for s in slices} == set(GERMANY["industries"])
    assert slices[0].area_id == "iso:DE-BY" and slices[0].industry_label == "Maschinenbau"
    assert plan.target == 1500                                 # tier B: 1.5x over-fetch
    assert sum(s.quota for s in slices) == 1500
    assert max(s.quota for s in slices) - min(s.quota for s in slices) <= 1


async def test_overfetch_by_tier() -> None:
    assert overfetch_target(1000, "B") == 1500
    assert overfetch_target(500, "C") == 1000
    assert overfetch_target(1, "A") == 2
    assert overfetch_target(100, "Z") == 200
    plan = build_plan(await resolved({**GERMANY, "country": "United States", "regions": ["California"],
                                      "industries": ["Manufacturing"], "max_output": 500}))
    assert plan.target == 1000


async def test_reallocation_when_slice_exhausted() -> None:
    plan = build_plan(await resolved({**GERMANY, "regions": ["Bayern"],
                                      "industries": ["Maschinenbau", "Logistik"], "max_output": 100}))
    keys = [slice_key(s) for s in plan.ordered()]
    assert [plan.quota(k) for k in keys] == [75, 75]
    plan.record(keys[0], 20)
    alloc = plan.mark_exhausted(keys[0])                       # slice ran dry after 20
    assert alloc == {keys[0]: 20, keys[1]: 130}
    assert [s.industry_label for s in plan.active] == ["Logistik"]


async def test_empty_regions_one_country_slice_per_industry() -> None:
    """One slice per DE first-level subdivision (16 per industry)."""
    plan = build_plan(await resolved({**GERMANY, "regions": []}))
    assert len(plan.ordered()) == 16 * 4
    assert len({s.area_id for s in plan.ordered()}) == 16
    assert all(s.area_id.startswith("iso:DE-") for s in plan.ordered())
    assert all(s.region_label == "" for s in plan.ordered())


async def test_inputs_resolving_to_same_profile_share_a_slice() -> None:
    plan = build_plan(await resolved({**GERMANY, "regions": ["Bayern"],
                                      "industries": ["Logistik", "Spedition"]}))
    assert [s.industry_label for s in plan.ordered()] == ["Logistik"]


def test_specificity_rankings() -> None:
    cat = default_catalog()
    maschinenbau = cat.resolve("Maschinenbau").profile
    produktion = cat.resolve("Produktion").profile
    lagerei = cat.resolve("Lagerei").profile
    logistik = cat.resolve("Logistik").profile
    assert industry_specificity(maschinenbau) > industry_specificity(produktion)
    assert industry_specificity(lagerei) > industry_specificity(logistik)
    ara = geo.resolve_region_detailed("FR", "Auvergne-Rhône-Alpes").area
    ain = geo.resolve_region_detailed("FR", "Ain").area
    assert ain.code == "FR-01"
    assert area_specificity(ain) > area_specificity(ara) > area_specificity(geo.country_area("FR"))


# --- country-wide requests → first-level subdivision slices ------------------------------------------
async def test_empty_regions_split_into_subdivisions_de() -> None:
    from leadscraper.sources.osm_overpass import build_query
    plan = build_plan(await resolved({**GERMANY, "regions": [], "industries": ["Logistik"]}))
    slices = plan.ordered()
    assert len(slices) == 16
    assert [s.area_id for s in slices][0] == "iso:DE-BB" and slices[-1].area_id == "iso:DE-TH"
    assert {"iso:DE-BW", "iso:DE-BY", "iso:DE-HB", "iso:DE-TH"} <= {s.area_id for s in slices}
    assert all(s.region_label == "" for s in slices)
    by = plan.areas["iso:DE-BY"]
    assert by.code == "DE-BY" and by.level != "country" and by.method == "country"
    query = build_query(by, plan.industries[slices[0].industry_profile_id], ("de",))
    assert 'area["ISO3166-2"="DE-BY"]->.region;' in query


async def test_country_with_too_many_or_no_subdivisions_keeps_country_slice() -> None:
    import pycountry

    from leadscraper.services.planner import country_split
    plan = build_plan(await resolved({**GERMANY, "country": "United States", "regions": [],
                                      "industries": ["Manufacturing"], "max_output": 10}))
    assert [s.area_id for s in plan.ordered()] == ["iso:US"]            # 51 > 40 after exclusions
    no_subs = next(c.alpha_2 for c in pycountry.countries
                   if not pycountry.subdivisions.get(country_code=c.alpha_2))
    assert country_split(geo.country_area(no_subs)) == []
    assert country_split(geo.resolve_region_detailed("DE", "Bayern").area) == []   # not a country


def test_country_split_excludes_dual_coded_territories() -> None:
    from leadscraper.services.planner import country_split
    nl = {a.code for a in country_split(geo.country_area("NL"))}
    fr = {a.code for a in country_split(geo.country_area("FR"))}
    assert len(nl) == 15 and not nl & {"NL-AW", "NL-CW", "NL-SX"}
    assert len(fr) == 19 and not fr & {"FR-BL", "FR-MF", "FR-NC", "FR-PF", "FR-PM", "FR-TF", "FR-WF"}


async def test_country_split_slices_per_industry_fr() -> None:
    plan = build_plan(await resolved({**GERMANY, "country": "France", "regions": [],
                                      "industries": ["logistique"], "max_output": 10}))
    assert len(plan.ordered()) == 19 and all(s.region_label == "" for s in plan.ordered())


def test_excluded_codes_are_valid() -> None:
    import pycountry

    from leadscraper import constants as C
    assert len(C.COUNTRY_SPLIT_EXCLUDED_CODES) == 19
    for code in C.COUNTRY_SPLIT_EXCLUDED_CODES:
        assert pycountry.subdivisions.get(code=code) is not None, code
        assert pycountry.countries.get(alpha_2=code.split("-")[1]) is not None, code
