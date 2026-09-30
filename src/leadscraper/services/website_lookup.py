"""Website lookup for candidates without a usable website tag."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from leadscraper import constants as C
from leadscraper.crawler.website import WebsiteRejected, normalize_website, registered_domain
from leadscraper.domain.models import CompanyCandidate
from leadscraper.extractors.identity import company_tokens, translit
from leadscraper.extractors.scoring import default_config, default_free_mail_predicate
from leadscraper.sources.web_search import SearchBackend, SearchGate, SearchJob


@dataclass(slots=True, frozen=True)
class WebsiteGuess:
    url: str
    method: str                      # email_domain | search | guess


#: registrable second-level domain for companies where the ccTLD itself is not used (GB: .co.uk)
_COMPANY_TLD = {"gb": "co.uk", "uk": "co.uk"}


def guess_domains(name: str, country_code: str, legal_form_tokens: Sequence[tuple[str, ...]],
                  generic_tokens: Iterable[str] = ()) -> list[str]:
    tokens = company_tokens(name, legal_form_tokens)[:3]
    generic = {t for g in generic_tokens for t in translit(g).split()}
    if not tokens or len("".join(tokens)) < C.GUESS_MIN_NAME_LEN or set(tokens) <= generic:
        return []
    cc = country_code.lower()
    tld = _COMPANY_TLD.get(cc, cc)
    hyphen, joined = "-".join(tokens), "".join(tokens)
    out = list(dict.fromkeys([f"{hyphen}.{tld}", f"{joined}.{tld}", f"{hyphen}.com"]))
    return out[:C.GUESS_MAX_DOMAINS]


def domain_label(url: str) -> str:
    """``https://www.spedition-mueller.de`` → ``spedition-mueller`` (first label of the registered
    domain)."""
    domain = registered_domain(url) or ""
    return domain.split(".", 1)[0]


def plausible_domain(name: str, url: str, legal_form_tokens: Sequence[tuple[str, ...]]) -> bool:
    """Domain plausibility."""
    tokens = company_tokens(name, legal_form_tokens)
    label = domain_label(url)
    if not tokens or not label:
        return False
    joined, flat = "".join(tokens), label.replace("-", "")
    if joined == flat or "".join(t[0] for t in tokens) == flat:
        return True
    token_hit = any(len(t) >= 3 and t in label for t in tokens)
    return token_hit and fuzz.partial_ratio(joined, label) >= C.LOOKUP_DOMAIN_MIN_SCORE


class WebsiteLookup:
    def __init__(self, *, is_free_mail: Callable[[str], bool] | None = None,
                 search_backend: SearchBackend | None = None, search_gate: SearchGate | None = None,
                 search_job: SearchJob | None = None, language: str = "en", country: str = "",
                 legal_form_tokens: Sequence[tuple[str, ...]] = (), guess: bool = False,
                 host_check: Callable[[str], Awaitable[bool]] | None = None,
                 generic_tokens: Iterable[str] = ()) -> None:
        self.is_free_mail = is_free_mail or default_free_mail_predicate(default_config())
        self.search_backend, self.search_gate = search_backend, search_gate
        self.search_job = search_job or SearchJob()
        self.language, self.country = language, country
        self.legal_form_tokens = legal_form_tokens
        self.guess_enabled = guess and host_check is not None
        self.host_check = host_check
        self.generic_tokens = tuple(generic_tokens)

    @property
    def search_enabled(self) -> bool:
        return self.search_backend is not None and self.search_gate is not None

    def email_domain(self, candidate: CompanyCandidate) -> WebsiteGuess | None:
        email = (candidate.hints.get("email") or "").strip().lower()
        _, at, host = email.rpartition("@")
        domain = registered_domain(host) if at else None
        if not domain or self.is_free_mail(host) or self.is_free_mail(domain):
            return None
        try:
            site = normalize_website(f"https://{domain}")            # rejects social/directory hosts
        except WebsiteRejected:
            return None
        return WebsiteGuess(site.url, "email_domain")

    def guesses(self, candidate: CompanyCandidate) -> list[WebsiteGuess]:
        """Websites that need no network to find."""
        guess = self.email_domain(candidate)
        return [guess] if guess else []

    def search_query(self, candidate: CompanyCandidate, area_name: str | None) -> str:
        place = (candidate.hints.get("city") or area_name or "").strip()
        return f'"{candidate.name}" {place}'.strip()

    async def search_guesses(self, candidate: CompanyCandidate,
                             area_name: str | None) -> list[WebsiteGuess]:
        """Plausible result domains of one search for the candidate (≤ ``LOOKUP_MAX_TRIES``)."""
        if not self.search_enabled:
            return []
        results = await self.search_gate.search(                         # type: ignore[union-attr]
            self.search_backend, self.search_query(candidate, area_name), language=self.language,
            country=self.country, job=self.search_job)
        out: list[WebsiteGuess] = []
        for result in results:
            if plausible_domain(candidate.name, result.url, self.legal_form_tokens):
                out.append(WebsiteGuess(result.url, "search"))
            if len(out) >= C.LOOKUP_MAX_TRIES:
                break
        return out

    async def guess(self, candidate: CompanyCandidate) -> list[WebsiteGuess]:
        """The first guessed domain whose host resolves to public addresses (DNS only, no HTTP for
        NXDOMAIN or private answers); ``[]`` if none does."""
        if not self.guess_enabled:
            return []
        for domain in guess_domains(candidate.name, self.country, self.legal_form_tokens,
                                    self.generic_tokens):
            if await self.host_check(domain):                        # type: ignore[misc]
                return [WebsiteGuess(f"https://{domain}/", "guess")]
        return []

