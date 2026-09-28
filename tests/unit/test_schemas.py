import pytest
from pydantic import ValidationError

from leadscraper.schemas.scrape import InformationField, ScrapeRequest
from leadscraper.schemas.verify import VerifyRequest

BASE = {"country": "Germany", "industries": ["Logistik"], "information": ["company_email"]}


def make(**kw):
    return ScrapeRequest.model_validate({**BASE, **kw})


def test_a21_examples_valid() -> None:
    bodies = [
        {"country": "Germany", "regions": ["Bayern", "Nordrhein-Westfalen", "Hessen"],
         "industries": ["Maschinenbau", "Logistik", "Großhandel", "Produktion"],
         "information": ["company_name", "company_email", "website"], "max_output": 1000},
        {"country": "France", "regions": ["Île-de-France", "Auvergne-Rhône-Alpes"],
         "industries": ["Transport routier", "Commerce de gros"],
         "information": ["company_name", "company_email", "website", "phone"], "max_output": 300},
        {"country": "United States", "regions": ["California", "Texas"],
         "industries": ["Manufacturing", "Logistics"],
         "information": ["company_name", "company_email", "website"], "max_output": 500},
    ]
    for body in bodies:
        req = ScrapeRequest.model_validate(body)
        assert req.max_output == body["max_output"]


def test_defaults() -> None:
    r = make()
    assert r.regions == [] and r.max_output == 100 and r.verify_emails is False
    assert r.freshness_days == 90 and r.exclude_marketing_objections is True
    assert r.callback_url is None
    assert r.information == [InformationField.COMPANY_EMAIL]


def test_all_information_values() -> None:
    values = ["company_name", "company_email", "website", "phone", "address", "legal_form",
              "register_number", "vat_id"]
    assert [v.value for v in make(information=values).information] == values
    with pytest.raises(ValidationError):
        make(information=["fax"])
    with pytest.raises(ValidationError):
        make(information=[])


@pytest.mark.parametrize("n,ok", [(0, True), (50, True), (51, False)])
def test_regions_bounds(n: int, ok: bool) -> None:
    regions = [f"R{i}" for i in range(n)]
    if ok:
        assert len(make(regions=regions).regions) == n
    else:
        with pytest.raises(ValidationError):
            make(regions=regions)


@pytest.mark.parametrize("n,ok", [(0, False), (1, True), (20, True), (21, False)])
def test_industries_bounds(n: int, ok: bool) -> None:
    industries = [f"I{i}" for i in range(n)]
    if ok:
        assert len(make(industries=industries).industries) == n
    else:
        with pytest.raises(ValidationError):
            make(industries=industries)


@pytest.mark.parametrize("value,ok", [(0, False), (1, True), (5000, True), (5001, False)])
def test_max_output_bounds(value: int, ok: bool) -> None:
    if ok:
        assert make(max_output=value).max_output == value
    else:
        with pytest.raises(ValidationError):
            make(max_output=value)


@pytest.mark.parametrize("value,ok", [(0, False), (1, True), (365, True), (366, False)])
def test_freshness_bounds(value: int, ok: bool) -> None:
    if ok:
        assert make(freshness_days=value).freshness_days == value
    else:
        with pytest.raises(ValidationError):
            make(freshness_days=value)


def test_strip_and_case_insensitive_dedupe() -> None:
    r = make(regions=[" Bayern ", "bayern", "BAYERN", "", "  ", "NRW"],
             industries=["Logistik", " logistik", "Großhandel"])
    assert r.regions == ["Bayern", "NRW"]
    assert r.industries == ["Logistik", "Großhandel"]


def test_industries_blank_only_rejected() -> None:
    with pytest.raises(ValidationError):
        make(industries=["  ", ""])


def test_country_length() -> None:
    with pytest.raises(ValidationError):
        make(country="D")
    with pytest.raises(ValidationError):
        make(country="x" * 101)


def test_callback_url() -> None:
    r = make(callback_url="http://n8n:5678/webhook/lead-result")
    assert str(r.callback_url) == "http://n8n:5678/webhook/lead-result"
    with pytest.raises(ValidationError):
        make(callback_url="not a url")


def test_verify_request_limits() -> None:
    assert VerifyRequest(emails=["a@b-example.de"]).smtp_check is False
    VerifyRequest(emails=["x"] * 10_000)
    with pytest.raises(ValidationError):
        VerifyRequest(emails=["x"] * 10_001)
    with pytest.raises(ValidationError):
        VerifyRequest(emails=[])
