"""Scrape pipeline orchestration."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import math
import re
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from selectolax.parser import HTMLParser

from leadscraper import constants as C
from leadscraper.crawler.fetcher import Fetcher, accept_language
from leadscraper.crawler.pages import Page, SiteCrawl, crawl_site
from leadscraper.crawler.robots import RobotsCache
from leadscraper.crawler.website import (
    HostGuard,
    Resolve,
    Website,
    WebsiteRejected,
    normalize_website,
    registered_domain,
    system_resolve,
    website_for,
)
from leadscraper.domain.models import CompanyCandidate, EmailFinding, JobStatus, SearchSlice
from leadscraper.extractors import address as address_x
from leadscraper.extractors.address import own_address
from leadscraper.extractors import legal as legal_x
from leadscraper.extractors import objection as objection_x
from leadscraper.extractors import phone as phone_x
from leadscraper.extractors.emails import extract_emails
from leadscraper.extractors.identity import matches_company, translit
from leadscraper.extractors.jsonld import JsonLdOrg, extract_organizations
from leadscraper.extractors.page_emails import extract_page_emails
from leadscraper.extractors.scoring import ScoredEmail, is_special_function, rank_emails
from leadscraper.extractors.text import page_text
from leadscraper.jobs.callback import deliver_callback
from leadscraper.jobs.manager import JobManager, JobState
from leadscraper.observability import metrics
from leadscraper.observability.logging import get_logger
from leadscraper.schemas.scrape import InformationField, ScrapeRequest
from leadscraper.services.dedup import DedupEntry, Deduplicator
from leadscraper.services.planner import build_plan, slice_key
from leadscraper.services.region_check import (
    Evidence,
    company_evidence,
    needs_evidence,
    normalise_postcode,
    region_confidence,
)
from leadscraper.services.resolver.industry import keywords_for
from leadscraper.services.resolver.resolve import ResolutionError, ResolvedRequest, Resolver
from leadscraper.services.verify_service import EmailVerifier
from leadscraper.services.website_lookup import WebsiteGuess, WebsiteLookup
from leadscraper.settings import Settings
from leadscraper.sources.osm_overpass import OverpassAdapter, OverpassGate, OverpassGatePool
from leadscraper.sources.base import SourceAdapter
from leadscraper.sources.registry import build_adapters
from leadscraper.sources.web_search import SOURCE_NAME as WEB_SEARCH
from leadscraper.sources.web_search import SearchBackend, SearchGate, SearchJob, WebSearchAdapter
from leadscraper.verification.lists import SuppressionList

log = get_logger(__name__)
F = InformationField
VerifierFactory = Callable[[Settings], EmailVerifier]
UNUSABLE_RESULTS = frozenset({"undeliverable", "suppressed"})
#: The one page-email extractor used by ``_findings`` and the early-exit predicate: the pass.
page_emails: Callable[[str], set[str]] = extract_page_emails
#: early exit is allowed only when nothing beyond these fields is requested.
EARLY_EXIT_FIELDS = frozenset({InformationField.COMPANY_NAME, InformationField.COMPANY_EMAIL,
                               InformationField.WEBSITE})


@dataclass
class PipelineDeps:
    settings: Settings
    resolver: Resolver
    gate: OverpassGate | OverpassGatePool                   # plain gate = single endpoint
    verifier_factory: VerifierFactory
    dns_resolve: Resolve = system_resolve                   # HostGuard (SSRF) lookups
    client_factory: Callable[[], httpx.AsyncClient] = field(default=lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(C.CRAWLER_HTTP_TIMEOUT_S, connect=C.CRAWLER_CONNECT_TIMEOUT_S,
                              read=C.CRAWLER_READ_TIMEOUT_S), follow_redirects=False))
    suppression: SuppressionList = field(default_factory=SuppressionList)
    callback_client_factory: Callable[[], httpx.AsyncClient] = field(default=lambda: httpx.AsyncClient(
        timeout=C.CALLBACK_TIMEOUT_S, follow_redirects=False))
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)   # timeout callbacks
    # deps built directly have web search off; create_app sets both.
    search_backend: SearchBackend | None = None
    search_gate: SearchGate | None = None
    website_guess: bool = C.WEBSITE_GUESS_ENABLED               # tests may switch it off
    # test seam: when set, replaces ``build_adapters`` (same arguments); tests only.
    adapter_factory: Callable[..., list[SourceAdapter]] | None = None


@dataclass(slots=True)
class Accepted:
    entry: DedupEntry
    record: dict[str, Any]


@dataclass
class SliceStats:
    candidates: int = 0
    crawled: int = 0
    with_email: int = 0


# --- extraction for one company -------------------------------------------------------------------
def _findings(crawl: SiteCrawl, orgs: list[JsonLdOrg], entry: DedupEntry) -> list[EmailFinding]:
    out: list[EmailFinding] = []
    ordered = sorted(crawl.pages, key=lambda p: (not p.is_legal, p.kind != "contact"))
    for page in ordered:                          # legal notice first
        for email in sorted(page_emails(page.html)):
            out.append(EmailFinding(email, page.url, on_legal_notice=page.is_legal))
    for org in orgs:
        for email in org.emails:
            out.append(EmailFinding(email, crawl.website.url, from_jsonld=True))
    hint = entry.candidate.hints.get("email")
    if hint:
        for email in extract_emails(f"mailto:{hint}"):
            out.append(EmailFinding(email, f"https://www.openstreetmap.org/{entry.candidate.source_ref}"))
    return out


@dataclass(slots=True)
class Extracted:
    ranked: list[ScoredEmail]
    fields: dict[str, Any]
    objection: str | None
    orgs: list[JsonLdOrg] = field(default_factory=list)      # JSON-LD
    texts: list[str] = field(default_factory=list)           # page texts, legal pages first


def legal_page_has_same_domain_email(pages: list[Page], *,
                                     is_suppressed: Callable[[str], bool] = lambda e: False) -> bool:
    """Predicate: a fetched legal-notice page yields a usable email on the site's own registered
    domain (legal pages are always on the site's domain, ``crawl_site`` drops off-site pages)."""
    for page in pages:
        if not page.is_legal:
            continue
        site = registered_domain(page.url)
        for email in page_emails(page.html):
            if (registered_domain(email.rpartition("@")[2]) == site
                    and not is_special_function(email) and not is_suppressed(email)):
                return True
    return False


def early_exit_allowed(req: ScrapeRequest) -> bool:
    """Only for name/email/website requests without email verification."""
    return set(req.information) <= EARLY_EXIT_FIELDS and not req.verify_emails


#: ``websites_resolved_total`` methods
WEB_DISCOVERY, WEB_DISCOVERY_MERGED = "web_discovery", "web_discovery_merged"
_UNSET: Any = object()


def web_company_name(texts: Sequence[str], orgs: Sequence[JsonLdOrg], cc: str,
                     serp_title: str) -> tuple[str, str | None] | None:
    """``(name, legal form)`` of a web-discovered company from a legal-name hit on its pages or its
    JSON-LD ``legalName``/``name``; ``None`` otherwise."""
    structured = [x for o in orgs for x in (o.legal_name, o.name) if x]
    hit = legal_x.extract_legal_name(texts, cc, candidates=structured, source_name=serp_title)
    if hit is not None:
        return hit.legal_name, hit.legal_form
    return (structured[0], None) if structured else None


def has_industry_keyword(texts: Sequence[str], keywords: Iterable[str]) -> bool:
    """One of the slice's industry keywords is on the page texts (transliterated, case-insensitive;
    a keyword shorter than ``OVERPASS_SUBSTRING_MIN_LEN`` must be a whole word, as in the
    Overpass query)."""
    hay = [translit(t) for t in texts]
    for keyword in keywords:
        k = translit(keyword).strip()
        if not k:
            continue
        if len(k) < C.OVERPASS_SUBSTRING_MIN_LEN:
            pattern = re.compile(rf"(?<!\w){re.escape(k)}(?!\w)")
            if any(pattern.search(h) for h in hay):
                return True
        elif any(k in h for h in hay):
            return True
    return False


def _set_web_name(ex: Extracted, name: tuple[str, str | None]) -> None:
    """The name replaces the fallback of ``extract_company`` (requested fields only)."""
    if "company_name" in ex.fields:
        ex.fields["company_name"] = name[0]
    if "legal_form" in ex.fields:
        ex.fields["legal_form"] = name[1]


def extract_company(crawl: SiteCrawl, entry: DedupEntry, req: ScrapeRequest,
                    resolved: ResolvedRequest) -> Extracted:
    """Run only the extractors the requested ``information`` needs."""
    info = set(req.information)
    cc, langs = resolved.country_code, resolved.profile.languages
    pages = sorted(crawl.pages, key=lambda p: (not p.is_legal, p.kind == "home"))
    texts = [page_text(p.html) for p in pages]
    orgs = [o for p in pages for o in extract_organizations(p.html)]
    fields: dict[str, Any] = {}
    ranked: list[ScoredEmail] = []
    if F.COMPANY_EMAIL in info:
        ranked = rank_emails(_findings(crawl, orgs, entry), website=crawl.website.origin,
                             languages=langs, country=cc)
    if F.COMPANY_NAME in info or F.LEGAL_FORM in info:
        candidates = [x for o in orgs for x in (o.legal_name, o.name) if x]
        if entry.candidate.source != WEB_SEARCH:                # never the SERP title
            candidates.append(entry.candidate.name)
        hit = legal_x.extract_legal_name(texts, cc, candidates=candidates,
                                         source_name=entry.candidate.name)
        fields["company_name"] = hit.legal_name if hit else entry.candidate.name
        fields["legal_form"] = hit.legal_form if hit else None
    if F.WEBSITE in info:
        fields["website"] = crawl.website.origin
    if F.PHONE in info:
        structured = [t for o in orgs for t in o.telephones]
        if entry.candidate.hints.get("phone"):
            structured.append(entry.candidate.hints["phone"])
        fields["phone"] = phone_x.extract_phone(texts, cc, structured=structured)
    if F.ADDRESS in info:
        addr = next((o.address_text for o in orgs if o.address_text), None)
        if addr is None:
            for text in texts:
                if found := address_x.extract_address(text, resolved.profile.postal_patterns, cc):
                    addr = found.address
                    break
        fields["address"] = addr
    if F.REGISTER_NUMBER in info:
        fields["register_number"] = next(
            (r for t in texts if (r := legal_x.extract_register_number(t, cc))), None)
    if F.VAT_ID in info:
        vat = next((legal_x.extract_vat_id(o.vat_id, cc) for o in orgs if o.vat_id), None)
        fields["vat_id"] = vat or next((v for t in texts if (v := legal_x.extract_vat_id(t, cc))), None)
    objection = objection_x.find_objection(texts) if req.exclude_marketing_objections else None
    return Extracted(ranked, fields, objection, orgs, texts)


def assemble_record(req: ScrapeRequest, entry: DedupEntry, fields: dict[str, Any],
                    email: str | None) -> dict[str, Any]:
    """Only the requested fields (in request order) + the three context labels."""
    record: dict[str, Any] = {}
    for f in req.information:
        record[f.value] = email if f is F.COMPANY_EMAIL else fields.get(f.value)
    record["country"] = req.country
    record["region"] = entry.region_label or None           # null when regions == []
    record["industry"] = entry.industry_label
    return record


# --- pipeline ---------------------------------------------------------------------------------------
class ScrapePipeline:
    def __init__(self, deps: PipelineDeps, jobs: JobManager, job: JobState) -> None:
        self.deps, self.jobs, self.job = deps, jobs, job
        self.settings = deps.settings
        self.req = ScrapeRequest.model_validate(job.request)
        self.accepted: list[Accepted] = []
        self.stop = asyncio.Event()
        self.stats: dict[str, SliceStats] = {}
        self.progress = {"target": self.req.max_output, "candidates": 0, "crawled": 0, "with_email": 0}
        self.seen_domains: set[str] = set()
        self.tasks: set[asyncio.Task[None]] = set()
        self.warnings: list[str] = []
        self.surplus: dict[str, deque[DedupEntry]] = {}       # usable, beyond the slice quota
        self.lookup_pool: dict[str, deque[DedupEntry]] = {}   # no website, no guess
        self.lookup = WebsiteLookup()                          # email domain
        self.search_job = SearchJob(budget=C.WEB_SEARCH_BUDGET_PER_JOB)   # job budget
        self.processed = 0                                    # candidates scheduled in this job
        self.osm_postcodes: dict[str, set[str]] = {}          # area id → OSM postcodes
        self.gazetteer_source: OverpassAdapter | None = None  # area gazetteer (lazy)
        self.web_adapter: WebSearchAdapter | None = None      # web-search discovery
        self.web_pool: dict[str, deque[CompanyCandidate]] = {}   # per slice, not yet deduplicated
        self.web_tasks: list[asyncio.Task[int]] = []          # first pass
        self._merge_claims: set[int] = set()                  # OSM entries a web record merges into

    # progress ------------------------------------------------------------------------------------
    def _publish(self) -> None:
        self.jobs.update_progress(self.job.job_id, count=len(self.accepted), **self.progress)
        if self.job.resolved is not None:
            self.job.resolved["known_in_job"] = len(self.accepted)

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            self._purge_intermediate()
            # max-runtime: the sweeper marks the job failed/job_timeout *before* cancelling it, so a
            # timed-out job counts as `failed`; a DELETE-cancelled job counts as `cancelled`.
            status = "failed" if self.job.status is JobStatus.FAILED else "cancelled"
            metrics.SCRAPE_JOBS.labels(status=status).inc()
            raise
        except ResolutionError as exc:
            self._purge_intermediate()
            self.jobs.fail(self.job.job_id, exc.code, exc.message, exc.details)
            metrics.SCRAPE_JOBS.labels(status="failed").inc()
        except Exception:
            self._purge_intermediate()
            metrics.SCRAPE_JOBS.labels(status="failed").inc()
            raise                                     # manager marks failed / internal_error
        else:
            metrics.SCRAPE_JOBS.labels(status="success").inc()

    def _purge_intermediate(self) -> None:
        for task in self.tasks:
            task.cancel()
        job = self.job
        for path in (job.candidates_path, job.result_path):
            path.unlink(missing_ok=True)
        shutil.rmtree(job.crawl_dir, ignore_errors=True)

    async def _run(self) -> None:
        resolved = await self.deps.resolver.resolve(self.req)
        self.resolved = resolved
        self.job.resolved = resolved.to_block(known_in_job=0)
        plan = build_plan(resolved)
        self.plan = plan
        self.dedup = Deduplicator(plan)
        self.verifier = self.deps.verifier_factory(self.settings) if self.req.verify_emails else None
        self._publish()
        self.job.crawl_dir.mkdir(parents=True, exist_ok=True)
        async with self.deps.client_factory() as client:
            fetcher = Fetcher(self.settings, client, HostGuard(self.deps.dns_resolve),
                              accept_language=accept_language(resolved.profile.languages,
                                                              resolved.country_code))
            RobotsCache(fetcher).attach()
            self.fetcher = fetcher
            generic = [w for ind in plan.industries.values()
                       for words in keywords_for(ind, resolved.profile.languages).values() for w in words]
            self.lookup = WebsiteLookup(search_backend=self.deps.search_backend,
                                        search_gate=self.deps.search_gate, search_job=self.search_job,
                                        language=(resolved.profile.languages or ("en",))[0],
                                        country=resolved.country_code, legal_form_tokens=self.dedup.forms,
                                        guess=self.deps.website_guess, host_check=fetcher.guard.is_public,
                                        generic_tokens=generic)
            self.sem = asyncio.Semaphore(self._company_concurrency())
            factory = self.deps.adapter_factory or build_adapters
            adapters = factory(self.settings, resolved.profile, gate=self.deps.gate,
                               areas=plan.areas, industries=plan.industries, client=client,
                               search_backend=self.deps.search_backend,
                               search_gate=self.deps.search_gate, search_job=self.search_job)
            self.gazetteer_source = next((a for a in adapters if isinstance(a, OverpassAdapter)), None)
            self.web_adapter = next((a for a in adapters if isinstance(a, WebSearchAdapter)), None)
            try:
                await self._discover_all(adapters)
                self._reallocate()
                await self._drain()
                await self._top_up()
            finally:
                for task in (*self.tasks, *self.web_tasks):     # never wait for search
                    task.cancel()
            for adapter in adapters:
                if isinstance(adapter, OverpassAdapter):
                    self.warnings.extend(w for w in adapter.warnings if w not in self.warnings)
        self._finish()

    async def _discover_all(self, adapters: list) -> None:
        """One task per slice, at most Σ endpoint concurrency active (2 for one endpoint)."""
        plan = self.plan
        taken: dict[str, int] = {k: 0 for k in plan.slices}
        self.surplus = {k: deque() for k in plan.slices}
        self.lookup_pool = {k: deque() for k in plan.slices}
        self.web_pool = {k: deque() for k in plan.slices}
        slice_adapters = [a for a in adapters if not isinstance(a, WebSearchAdapter)]
        slots = asyncio.Semaphore(self._slice_concurrency())
        with self.job.candidates_path.open("a", encoding="utf-8") as out:

            async def run_slice(s: SearchSlice) -> None:
                async with slots:
                    key = slice_key(s)
                    self.stats.setdefault(key, SliceStats())
                    for adapter in slice_adapters:
                        if self.stop.is_set():
                            return
                        async for cand in adapter.discover(s):
                            if cand.source == "osm" and cand.postal_code:
                                self.osm_postcodes.setdefault(s.area_id, set()).add(
                                    normalise_postcode(cand.postal_code))
                            out.write(json.dumps({"slice": key, **dataclasses.asdict(cand)},
                                                 ensure_ascii=False) + "\n")
                            res = self.dedup.add(cand, s)
                            if not res.new:
                                continue
                            self.progress["candidates"] += 1
                            self.stats[key].candidates += 1
                            if not self._usable(res.entry):
                                self.lookup_pool[key].append(res.entry)   # not counted
                            elif taken[key] < plan.quota(key):
                                taken[key] += 1
                                self._schedule(res.entry)
                            else:
                                self.surplus[key].append(res.entry)
                            if self.stop.is_set():
                                return
                        out.flush()
                    plan.record(key, taken[key])
                    self._publish()

            # the first discovery query per slice runs concurrently with Overpass (through the
            # shared SearchGate, outside the Overpass slots); it is never awaited here
            if self.web_adapter is not None:
                self.web_tasks = [asyncio.create_task(self._web_query(s)) for s in plan.ordered()]
            tasks = [asyncio.create_task(run_slice(s)) for s in plan.ordered()]
            watcher = asyncio.create_task(self._cancel_on_stop([*tasks, *self.web_tasks]))
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                watcher.cancel()
                for task in tasks:
                    task.cancel()
            for result in results:
                if isinstance(result, Exception):
                    raise result                     # an unexpected discovery error fails the job

    def _slice_concurrency(self) -> int:
        return OverpassGatePool.of(self.deps.gate, self.settings.overpass_url).concurrency

    async def _cancel_on_stop(self, tasks: list[asyncio.Task[None]]) -> None:
        await self.stop.wait()
        for task in tasks:
            task.cancel()

    def _usable(self, entry: DedupEntry) -> bool:
        """The candidate has a website that can be crawled (checked without network): its own tag
        (tier 0), or a email-domain guess (tier 1; the adapter yields those after tier 0)."""
        try:
            website_for(entry.candidate)
        except WebsiteRejected:
            return bool(self.lookup.guesses(entry.candidate))
        return True

    def _identity_ok(self, entry: DedupEntry, crawl: SiteCrawl, *, method: str) -> bool:
        """A looked-up website must name the company (and its location) itself."""
        if crawl.skipped or not crawl.pages:
            return False
        texts = [page_text(p.html) for p in crawl.pages]
        strict = method in ("search", "guess")
        own = None
        if strict:
            legal_texts = [t for p, t in zip(crawl.pages, texts) if p.is_legal]
            orgs = [o for p in crawl.pages for o in extract_organizations(p.html)]
            own = own_address(legal_texts, orgs, self.resolved.profile.postal_patterns,
                              self.resolved.country_code)
        titles = [t.text() for p in crawl.pages if (t := HTMLParser(p.html).css_first("title"))]
        cand = entry.candidate
        industry = self.plan.industries[entry.slice.industry_profile_id]
        generic = [w for words in keywords_for(industry, self.resolved.profile.languages).values()
                   for w in words]
        area = self.plan.areas.get(entry.slice.area_id)
        match = matches_company(texts, name=cand.name, postal_code=cand.postal_code,
                                city=cand.hints.get("city"), area_name=area.name if area else None,
                                country_code=self.resolved.country_code,
                                legal_form_tokens=self.dedup.forms, generic_tokens=generic,
                                titles=titles, require_location=method == "guess",
                                own_address=own, strict_location=strict)
        if not match.ok:
            log.info("website_lookup_rejected", domain=crawl.website.registered_domain,
                     reasons=list(match.reasons), name_score=round(match.name_score, 1))
        return match.ok

    async def _drain(self) -> None:
        """Wait for the scheduled companies."""
        if not self.tasks:
            return
        stopper = asyncio.ensure_future(self.stop.wait())
        try:
            while pending := [t for t in self.tasks if not t.done()]:
                if self.stop.is_set():
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    break
                await asyncio.wait([*pending, stopper], return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()

    def _company_concurrency(self) -> int:
        """Companies in flight; connections in flight stay CRAWLER_GLOBAL_CONCURRENCY (the Fetcher's
        own semaphore), so sleeping through a per-domain delay no longer idles a connection slot."""
        return C.CRAWLER_COMPANY_CONCURRENCY_FACTOR * self.settings.crawler_global_concurrency

    def _topup_min_batch(self) -> int:
        return self._company_concurrency()

    def _round_robin(self, size: int, pool: dict[str, deque[DedupEntry]] | None = None) -> list[DedupEntry]:
        """Up to ``size`` entries of ``pool`` (default: the surplus), one per slice in plan order
        per turn."""
        pool = self.surplus if pool is None else pool
        order = [slice_key(s) for s in self.plan.ordered()]
        batch: list[DedupEntry] = []
        while len(batch) < size and any(pool.get(k) for k in order):
            for k in order:
                if len(batch) < size and pool.get(k):
                    batch.append(pool[k].popleft())
        return batch

    def _lookups_possible(self) -> bool:
        """A lookup-pool candidate can still get a website (search on and not exhausted, or domain
        guessing on)."""
        searchable = (self.lookup.search_enabled and not self.search_job.disabled
                      and not self.search_job.exhausted)
        return searchable or self.lookup.guess_enabled

    def _reallocate(self) -> None:
        """Immediate re-allocation right after discovery, before the first crawl round drains: fill
        the discovery target from the surplus that exists, so dense slices take over the share of
        sparse ones without waiting a round."""
        missing = self.plan.target - self.processed
        if missing > 0 and not self.stop.is_set():
            for entry in self._round_robin(missing):
                self._schedule(entry)

    async def _top_up(self) -> None:
        """Top-up: while the target is not met, schedule surplus candidates round-robin across
        slices, batch by batch, up to the processed cap."""
        cap = C.TOPUP_MAX_PROCESSED_FACTOR * self.req.max_output
        factor = C.OVERFETCH_FACTOR_BY_TIER.get(self.resolved.profile.tier, C.OVERFETCH_FACTOR_DEFAULT)
        while (not self.stop.is_set() and len(self.accepted) < self.req.max_output
               and self.processed < cap):
            missing = self.req.max_output - len(self.accepted)
            size = min(max(math.ceil(missing * factor), self._topup_min_batch()), cap - self.processed)
            lookup = False
            if any(self.surplus.values()):
                batch = self._round_robin(size, self.surplus)
            elif await self._web_candidates():
                batch = self._admit_web(size)
                if not batch:                            # all duplicates of known companies
                    continue
            elif self._lookups_possible() and any(self.lookup_pool.values()):
                batch, lookup = self._round_robin(size, self.lookup_pool), True
            else:
                break
            for entry in batch:
                self._schedule(entry, lookup=lookup)
            await self._drain()
            self._publish()

    # web-search discovery
    async def _web_query(self, s: SearchSlice) -> int:
        """The slice's next discovery query into its web pool; the number of new candidates."""
        assert self.web_adapter is not None
        key, added = slice_key(s), 0
        async for cand in self.web_adapter.discover(s):
            domain = registered_domain(cand.website)
            if domain and (self.dedup.known_domain(domain) or domain in self.seen_domains):
                log.info("web_discovery_duplicate", domain=domain, reason="domain")
                continue
            self.web_pool[key].append(cand)
            added += 1
        return added

    async def _web_candidates(self) -> bool:
        """Web candidates are next: the first-pass results — awaited only here, when the target is
        not met and the surplus is used up — else, in top-up rounds only, further discovery
        queries (≤ ``WEB_DISCOVERY_QUERIES_PER_SLICE`` per slice, ≤ the budget share)."""
        if self.web_adapter is None or self.stop.is_set():
            return False
        if any(self.web_pool.values()):
            return True
        pending = [t for t in self.web_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending)
            if any(self.web_pool.values()):
                return True
        for s in self.plan.ordered():
            while not self.stop.is_set() and self.web_adapter.has_more(s):
                if await self._web_query(s):
                    return True
        return False

    def _admit_web(self, size: int) -> list[DedupEntry]:
        """Up to ``size`` web candidates, round-robin across slices, deduplicated only now (after
        OSM discovery), so the OSM entry always wins a shared domain (step 6a)."""
        order = [slice_key(s) for s in self.plan.ordered()]
        batch: list[DedupEntry] = []
        lines: list[str] = []
        while len(batch) < size and any(self.web_pool.get(k) for k in order):
            for k in order:
                if len(batch) >= size or not self.web_pool.get(k):
                    continue
                cand = self.web_pool[k].popleft()
                domain = registered_domain(cand.website)
                if not domain or self.dedup.known_domain(domain) or domain in self.seen_domains:
                    log.info("web_discovery_duplicate", domain=domain, reason="domain")
                    continue
                res = self.dedup.add(cand, self.plan.slices[k].slice)
                if not res.new:
                    continue
                self.progress["candidates"] += 1
                self.stats.setdefault(k, SliceStats()).candidates += 1
                lines.append(json.dumps({"slice": k, **dataclasses.asdict(cand)}, ensure_ascii=False))
                batch.append(res.entry)
        if lines:
            with self.job.candidates_path.open("a", encoding="utf-8") as out:
                out.write("\n".join(lines) + "\n")
        return batch

    def _schedule(self, entry: DedupEntry, *, lookup: bool = False) -> None:
        self.processed += 1
        task = asyncio.create_task(self._process(entry, lookup=lookup))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _process(self, entry: DedupEntry, *, lookup: bool = False) -> None:
        try:
            await self._process_inner(entry, lookup=lookup)
        except asyncio.CancelledError:
            raise
        except Exception:                                # one broken site never fails the job
            log.exception("company_processing_failed", source_ref=entry.candidate.source_ref)

    async def _process_inner(self, entry: DedupEntry, *, lookup: bool = False) -> None:
        async with self.sem:
            if self.stop.is_set():
                return
            try:
                tries: list[tuple[Website, WebsiteGuess | None]] = [(website_for(entry.candidate), None)]
            except WebsiteRejected:
                guesses = self.lookup.guesses(entry.candidate)     # hook, before crawl_site
                if not guesses and lookup:                         # top-up lookup rounds only
                    area = self.plan.areas.get(entry.slice.area_id)
                    guesses = await self.lookup.search_guesses(entry.candidate,
                                                               area.name if area else None)
                tries = []
                for guess in guesses:
                    try:
                        tries.append((normalize_website(guess.url), guess))
                    except WebsiteRejected:
                        continue
            for site, guess in tries:                              # the first verified site wins
                if self.stop.is_set() or await self._crawl_try(entry, site, guess):
                    return
            if lookup and not self.stop.is_set():                  # guess after search
                tried = {site.registered_domain for site, _ in tries}
                for guess in await self.lookup.guess(entry.candidate):
                    site = normalize_website(guess.url)
                    if site.registered_domain in tried:            # its search result was rejected
                        continue
                    if self.stop.is_set() or await self._crawl_try(entry, site, guess):
                        return

    async def _crawl_try(self, entry: DedupEntry, site: Website, looked_up: WebsiteGuess | None) -> bool:
        """Crawl one website for the candidate."""
        if site.registered_domain in self.seen_domains:
            return looked_up is None
        self.seen_domains.add(site.registered_domain)
        crawl = await crawl_site(self.fetcher, site, keywords=self.resolved.profile.contact_keywords,
                                 fallback_paths=self.deps.resolver.profiles.fallback_paths,
                                 legal_markers=self.deps.resolver.profiles.contact_pages.legal_markers,
                                 save_dir=self.job.crawl_dir,
                                 deadline=time.monotonic() + C.CRAWL_SITE_BUDGET_S,
                                 max_consecutive_failures=C.CRAWL_MAX_CONSECUTIVE_FAILURES,
                                 fallback_by_kind=self.deps.resolver.profiles.fallback_paths_for(
                                     self.resolved.profile.languages),
                                 contact_markers=self.deps.resolver.profiles.contact_pages.contact_markers,
                                 seed_variants=True, use_sitemap=True,
                                 done=functools.partial(legal_page_has_same_domain_email,
                                                        is_suppressed=self.deps.suppression.is_suppressed)
                                 if early_exit_allowed(self.req) else None)
        if looked_up is not None and not self._identity_ok(entry, crawl, method=looked_up.method):
            # the domain may still be another candidate's real website (e.g. a tagged one from a
            # slice that finishes later) → release it: a failed lookup is not "crawled"
            self.seen_domains.discard(site.registered_domain)
            self._publish()
            return False
        key = slice_key(entry.slice)
        self.progress["crawled"] += 1
        self.stats.setdefault(key, SliceStats()).crawled += 1
        if crawl.skipped or not crawl.pages or self.stop.is_set():
            self._publish()
            return True
        final_domain = crawl.website.registered_domain
        if final_domain != site.registered_domain:
            if final_domain in self.seen_domains:        # redirected onto a known company
                self._publish()
                return True
            self.seen_domains.add(final_domain)
        method = looked_up.method if looked_up else (
            WEB_DISCOVERY if entry.candidate.source == WEB_SEARCH else "osm_tag")
        result = await self._validate_and_assemble(entry, crawl, method)
        if result is not None and not self.stop.is_set():
            owner, record, method = result                # owner: the OSM entry of a merge
            self.accepted.append(Accepted(owner, record))
            metrics.WEBSITES_RESOLVED.labels(method=method).inc()
            if record.get("company_email"):
                self.progress["with_email"] += 1
                self.stats.setdefault(key, SliceStats()).with_email += 1
            if len(self.accepted) >= self.req.max_output:
                self.stop.set()                      # target met → stop early
        self._publish()
        return True

    async def _validate_and_assemble(self, entry: DedupEntry, crawl: SiteCrawl, method: str
                                     ) -> tuple[DedupEntry, dict[str, Any], str] | None:
        """``(owner entry, record, method)`` or ``None``."""
        ex = extract_company(crawl, entry, self.req, self.resolved)
        owner = entry
        if entry.candidate.source == WEB_SEARCH:
            decision = await self._web_decision(entry, crawl, ex)
            if decision is None:
                return None
            owner, method = decision
        elif await self._region_confidence(entry, crawl, ex) == "low":   # excluded by default
            return None
        if ex.objection:
            log.info("marketing_objection", domain=crawl.website.registered_domain)
            return None
        email: str | None = None
        if F.COMPANY_EMAIL in self.req.information:
            email = await self._choose_email(ex.ranked)
            if email is None:
                return None                              # not counted toward max_output
        return owner, assemble_record(self.req, owner, ex.fields, email), method

    async def _web_decision(self, entry: DedupEntry, crawl: SiteCrawl,
                            ex: Extracted) -> tuple[DedupEntry, str] | None:
        """For a web-discovered site: (a legal-name or JSON-LD name; an industry keyword on the home
        or legal page), then the name + postcode match against the area's OSM entries, else a new
        record under rule 3."""
        cc = self.resolved.country_code
        domain = crawl.website.registered_domain
        name = web_company_name(ex.texts, ex.orgs, cc, entry.candidate.name)
        if name is None:
            log.info("web_discovery_rejected", domain=domain, reason="no_legal_name")
            return None
        industry = self.plan.industries[entry.slice.industry_profile_id]
        keywords = [w for words in keywords_for(industry, self.resolved.profile.languages).values()
                    for w in words]
        scope = [page_text(p.html) for p in crawl.pages if p.kind == "home" or p.is_legal]
        if not has_industry_keyword(scope, keywords):
            log.info("web_discovery_rejected", domain=domain, reason="no_industry_keyword")
            return None
        legal = [page_text(p.html, drop_footer=True) for p in crawl.pages if p.is_legal]
        evidence = company_evidence(legal, ex.orgs, self.resolved.profile.postal_patterns, cc)
        osm = self.dedup.match_name(name[0], evidence.postcode if evidence else None,
                                    source="osm", area_id=entry.slice.area_id)
        if osm is not None:
            if (osm.candidate.website or id(osm) in self._merge_claims
                    or any(a.entry is osm for a in self.accepted)):
                log.info("web_discovery_duplicate", domain=domain, reason="name",
                         osm=osm.candidate.source_ref)
                return None                              # the OSM company has its own record/site
            self._merge_claims.add(id(osm))              # an identity decision for *that* entry
            if self._identity_ok(osm, crawl, method="search"):
                self._leave_lookup_pool(osm)
                _set_web_name(ex, name)
                return osm, WEB_DISCOVERY_MERGED
            self._merge_claims.discard(id(osm))
            log.info("web_discovery_not_merged", domain=domain, osm=osm.candidate.source_ref)
        if await self._region_confidence(entry, crawl, ex, evidence=evidence) == "low":
            return None
        _set_web_name(ex, name)
        return entry, WEB_DISCOVERY

    def _leave_lookup_pool(self, osm: DedupEntry) -> None:
        """A merged OSM entry gets no search/guess any more."""
        for key, pool in self.lookup_pool.items():
            if any(e is osm for e in pool):
                self.lookup_pool[key] = deque(e for e in pool if e is not osm)

    async def _region_confidence(self, entry: DedupEntry, crawl: SiteCrawl, ex: Extracted, *,
                                 evidence: Evidence | None | object = _UNSET) -> str:
        """The source-agnostic region check (``services/region_check``)."""
        cand = entry.candidate
        area = self.plan.areas.get(entry.slice.area_id)
        if not needs_evidence(cand) or area is None:
            return region_confidence(cand, area=area, evidence=None, gazetteer=None, osm_postcodes=())
        if evidence is _UNSET:
            legal = [page_text(p.html, drop_footer=True) for p in crawl.pages if p.is_legal]
            evidence = company_evidence(legal, ex.orgs, self.resolved.profile.postal_patterns,
                                        self.resolved.country_code)
        assert evidence is None or isinstance(evidence, Evidence)
        known = self.osm_postcodes.get(area.id, set())
        gazetteer = None
        decided = evidence is not None and bool(evidence.postcode) and \
            normalise_postcode(evidence.postcode or "") in known
        if evidence is not None and not decided and self.gazetteer_source is not None:
            gazetteer = await self.gazetteer_source.gazetteer(area)
        confidence = region_confidence(cand, area=area, evidence=evidence, gazetteer=gazetteer,
                                       osm_postcodes=known)
        if confidence == "low":
            log.info("region_check_rejected", source=cand.source, area=area.id,
                     evidence=evidence is not None, gazetteer=gazetteer is not None)
        return confidence

    async def _choose_email(self, ranked: list[ScoredEmail]) -> str | None:
        for scored in ranked:
            address = scored.email
            if self.deps.suppression.is_suppressed(address):
                continue
            if self.verifier is not None:
                result = await self.verifier.verify(address, smtp_check=False)
                if result.result.value in UNUSABLE_RESULTS:
                    continue                             # fall back to the next-best email
            return address
        return None

    def _finish(self) -> None:
        companies = [a.record for a in self.accepted[:self.req.max_output]]
        # labels may have been re-assigned by dedup after acceptance → refresh context labels
        for a, rec in zip(self.accepted, companies):
            rec["region"] = a.entry.region_label or None
            rec["industry"] = a.entry.industry_label
        self.job.result_path.write_text(json.dumps(
            {"status": "success", "job_id": self.job.job_id, "count": len(companies),
             "companies": companies}, ensure_ascii=False), encoding="utf-8")
        self.warnings.extend(w for w in self.search_job.warnings if w not in self.warnings)
        if self.job.resolved is not None:
            self.job.resolved["warnings"] = list(self.job.resolved.get("warnings", [])) + self.warnings
        self.job.warnings = self.warnings
        for key, st in self.stats.items():
            sp = self.plan.slices.get(key)
            if sp is None or st.crawled == 0:
                continue
            metrics.EMAIL_FOUND_RATIO.labels(country=self.resolved.country_code,
                                             region=sp.slice.area_id,
                                             industry=sp.slice.industry_profile_id
                                             ).set(st.with_email / st.crawled)
        self.jobs.set_result(self.job.job_id, companies, count=len(companies))
        self._publish()
        self.jobs.set_status(self.job.job_id, JobStatus.SUCCESS)


def runner_for(deps: PipelineDeps, jobs: JobManager) -> Callable[[JobState], Awaitable[None]]:
    """``JobManager.start(job, runner_for(deps, jobs))``: pipeline, then optional callback."""
    async def run(job: JobState) -> None:
        try:
            await ScrapePipeline(deps, jobs, job).run()
        except asyncio.CancelledError:
            # timeout (sweeper: failed/job_timeout, then cancel) → still call back once.
            if job.callback_url and jobs.get(job.job_id) is job and job.status is JobStatus.FAILED:
                task = asyncio.create_task(deliver_callback(
                    job, jobs, user_agent=deps.settings.crawler_user_agent,
                    client_factory=deps.callback_client_factory), name=f"callback:{job.job_id}")
                deps.background_tasks.add(task)          # kept reference: survives our cancellation
                task.add_done_callback(deps.background_tasks.discard)
            raise
        except Exception as exc:                         # same envelope the manager would set
            log.exception("job_failed", job_id=job.job_id)
            jobs.fail(job.job_id, "internal_error", "Job failed unexpectedly",
                      {"error": type(exc).__name__})
        if job.callback_url and jobs.get(job.job_id) is job:
            await deliver_callback(job, jobs, user_agent=deps.settings.crawler_user_agent,
                                   client_factory=deps.callback_client_factory)
    return run
