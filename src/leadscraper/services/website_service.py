"""``GET /contacts`` and ``POST /websites/find``"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from selectolax.parser import HTMLParser

from leadscraper import constants as C
from leadscraper.crawler.fetcher import Fetcher, accept_language
from leadscraper.crawler.pages import SiteCrawl, crawl_site
from leadscraper.crawler.robots import RobotsCache
from leadscraper.crawler.website import HostGuard, WebsiteRejected, normalize_website, registered_domain
from leadscraper.domain.models import CompanyCandidate, EmailFinding
from leadscraper.extractors.address import own_address
from leadscraper.extractors.identity import matches_company
from leadscraper.extractors.jsonld import extract_organizations
from leadscraper.extractors.page_emails import extract_page_emails
from leadscraper.extractors.phone import phones_in_text
from leadscraper.extractors.scoring import is_special_function, rank_emails
from leadscraper.extractors.socials import PLATFORMS, page_socials
from leadscraper.extractors.text import page_text
from leadscraper.observability.logging import get_logger
from leadscraper.services.dedup import legal_form_tokens
from leadscraper.services.resolver.profile import CountryProfile
from leadscraper.services.website_lookup import WebsiteGuess, WebsiteLookup, guess_domains
from leadscraper.sources.web_search import SearchJob

log = get_logger(__name__)

MODES = ("homepage", "key_pages", "deep")
_MODE_PAGES = {"homepage": 1, "key_pages": None, "deep": C.CONTACTS_DEEP_MAX_PAGES}
#: ccTLDs that are not the ISO code of their country.
_TLD_COUNTRY = {"uk": "GB", "eu": None}


def country_from_domain(domain: str | None) -> str | None:
    """``acme.de`` → ``DE``; generic TLDs (``.com``) → ``None``."""
    tld = (domain or "").rsplit(".", 1)[-1].lower()
    if tld in _TLD_COUNTRY:
        return _TLD_COUNTRY[tld]
    return tld.upper() if len(tld) == 2 and tld.isalpha() else None


def empty_output(domain: str | None, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"domain": domain, "title": None, "description": None,
                           "emails": [], "phones": []}
    out.update({p: [] for p in PLATFORMS})
    out["error"] = error
    return out


def _meta(html: str) -> tuple[str | None, str | None]:
    tree = HTMLParser(html)
    title = tree.css_first("title")
    desc = (tree.css_first('meta[name="description"]') or tree.css_first('meta[property="og:description"]'))
    t = " ".join(title.text().split()) if title else None
    d = " ".join((desc.attributes.get("content") or "").split()) if desc else None
    return t or None, d or None


@dataclass(slots=True)
class _Crawler:
    """One polite crawler for one request (per-request state only)."""

    deps: Any                                   # PipelineDeps (avoid an import cycle)
    profiles: Any                               # ProfileBuilder

    async def crawl(self, url: str, profile: CountryProfile, *, max_pages: int | None,
                    budget_s: float) -> SiteCrawl:
        settings = self.deps.settings
        if max_pages is not None:
            settings = settings.model_copy(update={"crawler_max_pages_per_domain": max_pages})
        async with self.deps.client_factory() as client:
            fetcher = Fetcher(settings, client, HostGuard(self.deps.dns_resolve),
                              accept_language=accept_language(profile.languages, profile.code))
            RobotsCache(fetcher).attach()
            return await crawl_site(
                fetcher, normalize_website(url), keywords=profile.contact_keywords,
                fallback_paths=self.profiles.fallback_paths,
                legal_markers=self.profiles.contact_pages.legal_markers,
                deadline=time.monotonic() + budget_s,
                max_consecutive_failures=C.CRAWL_MAX_CONSECUTIVE_FAILURES,
                fallback_by_kind=self.profiles.fallback_paths_for(profile.languages),
                contact_markers=self.profiles.contact_pages.contact_markers,
                seed_variants=True, use_sitemap=True)


def contacts_from_crawl(crawl: SiteCrawl, profile: CountryProfile, *,
                        is_suppressed=lambda e: False) -> dict[str, Any]:
    """The ``GET /contacts`` body for a finished crawl."""
    domain = crawl.website.host if crawl.website else None
    if crawl.skipped or not crawl.pages:
        return empty_output(domain, error=f"unreachable: {crawl.skipped or 'no pages fetched'}")
    site_domain = crawl.website.registered_domain
    findings: list[EmailFinding] = []
    sources: dict[str, list[str]] = {}
    for page in crawl.pages:
        for email in sorted(extract_page_emails(page.html)):
            findings.append(EmailFinding(email, page.url, on_legal_notice=page.is_legal))
            sources.setdefault(email.lower(), [])
            if page.url not in sources[email.lower()]:
                sources[email.lower()].append(page.url)
        for org in extract_organizations(page.html):
            for email in org.emails:
                findings.append(EmailFinding(email, page.url, from_jsonld=True))
                sources.setdefault(email.lower(), [])
                if page.url not in sources[email.lower()]:
                    sources[email.lower()].append(page.url)
    ranked = [r for r in rank_emails(findings, website=crawl.website.origin,
                                     languages=profile.languages, country=profile.code)
              if not is_suppressed(r.email)]
    emails = [{"value": r.email, "sources": sources.get(r.email.lower(), []),
               "is_likely_official": registered_domain("https://" + r.email.rpartition("@")[2]) == site_domain
               and not is_special_function(r.email)}
              for r in ranked]
    phones: list[str] = []
    for page in sorted(crawl.pages, key=lambda p: (not p.is_legal, p.kind != "contact")):
        text = page_text(page.html)
        for org in extract_organizations(page.html):
            text += "\n" + "\n".join(org.telephones)
        for number in phones_in_text(text, profile.code):
            if number not in phones:
                phones.append(number)
    title, description = _meta(crawl.pages[0].html)
    out = empty_output(domain)
    out.update({"title": title, "description": description, "emails": emails, "phones": phones})
    for platform in PLATFORMS:
        found: dict[str, dict[str, Any]] = {}
        for page in crawl.pages:
            for url, from_href in page_socials(page.html).get(platform, []):
                item = found.setdefault(url, {"value": url, "sources": [], "_href": False,
                                              "_home": False})
                if page.url not in item["sources"]:
                    item["sources"].append(page.url)
                item["_href"] |= from_href
                item["_home"] |= page.kind == "home"
        ordered = sorted(found.values(), key=lambda i: (-len(i["sources"]), not i["_home"]))
        out[platform] = [{"value": i["value"], "sources": i["sources"],
                          "is_likely_official": n == 0 and (i["_href"] or len(ordered) == 1)}
                         for n, i in enumerate(ordered)]
    return out


async def website_contacts(deps: Any, resolver: Any, website: str, *, mode: str = "key_pages",
                           country: str | None = None) -> dict[str, Any]:
    try:
        site = normalize_website(website)
    except WebsiteRejected as exc:
        return empty_output(website, error=f"invalid website: {exc.reason}")
    cc = resolver.resolve_country(country).code if country else (
        country_from_domain(site.registered_domain) or C.CONTACTS_DEFAULT_COUNTRY)
    profile = resolver.profiles.get(cc)
    budget = C.CONTACTS_DEEP_BUDGET_S if mode == "deep" else C.CRAWL_SITE_BUDGET_S
    crawl = await _Crawler(deps, resolver.profiles).crawl(site.url, profile,
                                                          max_pages=_MODE_PAGES[mode], budget_s=budget)
    return contacts_from_crawl(crawl, profile, is_suppressed=deps.suppression.is_suppressed)


_CONTEXT_POSTCODE = ("postcode", "postal_code", "zip", "zip_code", "plz")


async def find_website(deps: Any, resolver: Any, name: str, context: dict[str, Any]) -> dict[str, Any]:
    """``{"website": origin | None, "method": "email_domain" | "search" | "guess" | None}``."""
    ctx = {str(k).lower(): str(v).strip() for k, v in context.items() if v not in (None, "")}
    cc = resolver.resolve_country(ctx["country"]).code if ctx.get("country") else C.CONTACTS_DEFAULT_COUNTRY
    profile = resolver.profiles.get(cc)
    postcode = next((ctx[k] for k in _CONTEXT_POSTCODE if ctx.get(k)), None)
    city = ctx.get("city") or ctx.get("town") or None
    hints = {k: v for k, v in (("city", city), ("email", ctx.get("email"))) if v}
    cand = CompanyCandidate(name=name, source="api", source_ref="", postal_code=postcode, hints=hints)
    forms = legal_form_tokens(cc)
    has_location = bool(postcode or city)
    lookup = WebsiteLookup(search_backend=deps.search_backend, search_gate=deps.search_gate,
                           search_job=SearchJob(budget=C.FIND_WEBSITE_SEARCH_BUDGET),
                           language=(profile.languages or ("en",))[0], country=cc,
                           legal_form_tokens=forms)
    crawler = _Crawler(deps, resolver.profiles)
    tried: set[str] = set()
    candidates = list(lookup.guesses(cand))
    candidates += await lookup.search_guesses(cand, city)
    if deps.website_guess:                        # every resolving guess, not only the first
        guard = HostGuard(deps.dns_resolve)
        for domain in guess_domains(name, cc, forms):
            if await guard.is_public(domain):
                candidates.append(WebsiteGuess(f"https://{domain}/", "guess"))
    for guess in candidates:
        domain = registered_domain(guess.url)
        if not domain or domain in tried:
            continue
        tried.add(domain)
        crawl = await crawler.crawl(guess.url, profile, max_pages=None, budget_s=C.CRAWL_SITE_BUDGET_S)
        if crawl.skipped or not crawl.pages:
            continue
        texts = [page_text(p.html) for p in crawl.pages]
        strict = has_location and guess.method in ("search", "guess")
        own = None
        if strict:
            legal = [t for p, t in zip(crawl.pages, texts) if p.is_legal]
            orgs = [o for p in crawl.pages for o in extract_organizations(p.html)]
            own = own_address(legal, orgs, profile.postal_patterns, cc)
        titles = [t.text() for p in crawl.pages if (t := HTMLParser(p.html).css_first("title"))]
        match = matches_company(texts, name=name, postal_code=postcode, city=city, area_name=None,
                                country_code=cc, legal_form_tokens=forms, generic_tokens=(),
                                titles=titles, require_location=False, own_address=own,
                                strict_location=strict)
        if match.ok:
            return {"website": crawl.website.origin, "method": guess.method}
        log.info("find_website_rejected", domain=domain, reasons=list(match.reasons))
    return {"website": None, "method": None}
