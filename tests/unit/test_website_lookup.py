"""Website lookup: domain plausibility, search guesses, domain guessing."""

import pytest

from leadscraper import constants as C
from leadscraper.domain.models import CompanyCandidate
from leadscraper.services.dedup import legal_form_tokens
from leadscraper.services.website_lookup import (WebsiteLookup, domain_label, guess_domains,
                                                 plausible_domain)
from leadscraper.sources.web_search import SearchJob, SearchResult

FORMS = legal_form_tokens("DE")


@pytest.mark.parametrize("name,url,ok", [
    ("Spedition Müller GmbH", "https://spedition-mueller.de", True),       # tokens in the label
    ("Spedition Müller GmbH", "https://www.mueller-bau.de", False),        # one shared word only
    ("Spedition Müller GmbH", "https://speditionmueller.de", True),        # concatenated
    ("Spedition Müller GmbH", "https://sm.de", True),                      # initials
    ("ABC Logistik", "https://abc.de", True),                              # initials/token
    ("Kranich Transporte", "https://kranich-transporte-example.de", True),
    ("Möwenflug Kurierdienst", "https://moewenflug-kurier-example.de", True),
    ("Kranich Transporte", "https://firma-01-example.de", False),
    ("Hanse Frachtkontor GmbH & Co. KG", "https://hanse-frachtkontor-example.de", True),
])
def test_domain_plausibility(name: str, url: str, ok: bool) -> None:
    assert plausible_domain(name, url, FORMS) is ok


def test_domain_label() -> None:
    assert domain_label("https://www.shop.spedition-mueller.co.uk/x") == "spedition-mueller"
    assert domain_label("not a url") == ""


class FakeGate:
    def __init__(self, results):
        self.results, self.queries = results, []

    async def search(self, backend, query, *, language, country, job):
        self.queries.append((query, language, country))
        return self.results


@pytest.mark.anyio
async def test_search_guesses_plausible_in_order_at_most_max_tries() -> None:
    results = [SearchResult("https://kranich-bau-example.de", "x"),
               SearchResult("https://kranich-transporte-example.de", "Kranich"),
               SearchResult("https://www.kranich-transporte.de", "Kranich"),
               SearchResult("https://kranichtransporte.de", "Kranich")]
    gate = FakeGate(results)
    lookup = WebsiteLookup(search_backend=object(), search_gate=gate, search_job=SearchJob(),
                           language="de", country="DE", legal_form_tokens=FORMS)
    cand = CompanyCandidate(name="Kranich Transporte", source="osm", source_ref="node/1",
                            hints={"city": "Bremen"})
    guesses = await lookup.search_guesses(cand, "Bremen")
    assert [g.url for g in guesses] == ["https://kranich-transporte-example.de", "https://www.kranich-transporte.de"]
    assert len(guesses) == C.LOOKUP_MAX_TRIES and {g.method for g in guesses} == {"search"}
    assert gate.queries == [('"Kranich Transporte" Bremen', "de", "DE")]
    no_city = CompanyCandidate(name="Kranich Transporte", source="osm", source_ref="node/2")
    assert lookup.search_query(no_city, "Nordrhein-Westfalen") == '"Kranich Transporte" Nordrhein-Westfalen'


@pytest.mark.anyio
async def test_search_off_gives_no_guesses() -> None:
    lookup = WebsiteLookup()
    cand = CompanyCandidate(name="Kranich Transporte", source="osm", source_ref="node/1")
    assert not lookup.search_enabled and await lookup.search_guesses(cand, "Bremen") == []


# --- domain guessing -----------------------------------------------------------------------------
def test_guess_slugs() -> None:
    assert guess_domains("Spedition Müller GmbH & Co. KG", "DE", FORMS) == [
        "spedition-mueller.de", "speditionmueller.de", "spedition-mueller.com"]
    # a generic word next to a distinctive one is kept in the slug
    assert guess_domains("Spedition Müller GmbH", "DE", FORMS, ["Spedition"])[0] == "spedition-mueller.de"
    # the first three tokens only; at most GUESS_MAX_DOMAINS
    three = guess_domains("Hanse Fracht Kontor Nord GmbH", "DE", FORMS)
    assert three == ["hanse-fracht-kontor.de", "hansefrachtkontor.de", "hanse-fracht-kontor.com"]
    assert len(three) <= C.GUESS_MAX_DOMAINS
    assert guess_domains("Thames Haulage Ltd", "GB", legal_form_tokens("GB"))[0] == "thames-haulage.co.uk"


def test_generic_name_not_guessed() -> None:
    assert guess_domains("Spedition GmbH", "DE", FORMS, ["Spedition", "Logistik"]) == []
    assert guess_domains("Spedition Logistik GmbH", "DE", FORMS, ["Spedition", "Logistik"]) == []
    assert guess_domains("ABC GmbH", "DE", FORMS) == []              # joined slug < GUESS_MIN_NAME_LEN


class FakeHosts:
    def __init__(self, public: set[str]) -> None:
        self.public, self.checked = public, []

    async def __call__(self, host: str) -> bool:
        self.checked.append(host)
        return host in self.public


def guess_lookup(hosts: FakeHosts, *, guess: bool = True) -> WebsiteLookup:
    return WebsiteLookup(country="DE", legal_form_tokens=FORMS, guess=guess, host_check=hosts,
                         generic_tokens=["Spedition"])


@pytest.mark.anyio
async def test_guess_crawls_first_host_that_resolves() -> None:
    hosts = FakeHosts({"speditionmueller.de", "spedition-mueller.com"})
    cand = CompanyCandidate(name="Spedition Müller GmbH", source="osm", source_ref="node/1")
    guesses = await guess_lookup(hosts).guess(cand)
    assert [(g.url, g.method) for g in guesses] == [("https://speditionmueller.de/", "guess")]
    assert hosts.checked == ["spedition-mueller.de", "speditionmueller.de"]    # stops at the first hit


@pytest.mark.anyio
async def test_guess_off_or_generic_name_checks_no_host() -> None:
    hosts = FakeHosts({"spedition-mueller.de"})
    cand = CompanyCandidate(name="Spedition Müller GmbH", source="osm", source_ref="node/1")
    assert await guess_lookup(hosts, guess=False).guess(cand) == []
    generic = CompanyCandidate(name="Spedition GmbH", source="osm", source_ref="node/2")
    assert await guess_lookup(hosts).guess(generic) == []
    assert hosts.checked == []
    assert not WebsiteLookup(guess=True).guess_enabled                  # no host check → no guessing
