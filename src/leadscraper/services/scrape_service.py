"""Scrape pipeline orchestration (ARCHITECTURE.md §3.3, §3.4, §3.5, §3.8, §2.3, §8).

resolve → plan → discover (streamed to ``candidates.jsonl``) → dedup → crawl (HTML saved to
``crawl/``) → extract (only the requested fields) → validate → verify (optional) → assemble
(``result.json``).

- Discovery runs slice by slice (one Overpass query per slice in v0.3, Q13). Each slice first gets
  its even water-filling share; surplus candidates are kept, and after all slices ran the target is
  re-allocated with the real capacities (``allocate``, A§3.5), so a dense slice takes over the
  quota a sparse slice could not fill.
- Crawl/extract runs concurrently with discovery (bounded by ``CRAWLER_GLOBAL_CONCURRENCY``; the
  Fetcher enforces per-domain politeness). The job stops early once ``max_output`` companies are
  accepted; remaining work is cancelled.
- Validate: OSM candidates lie inside the requested area by construction (Overpass area filter,
  Q13); records without a trustworthy location get ``region_confidence="low"`` and are excluded
  (A§3.8). Companies with a marketing objection are dropped when
  ``exclude_marketing_objections`` (A§2.1). Suppressed addresses (operator file) are never used.
- Verify (``verify_emails=true``): syntax + DNS only; an ``undeliverable``/``suppressed`` best
  email is replaced by the next-best one, otherwise the company is dropped (Q15). No verification
  fields appear in the output (A§2.3).
- Assemble: exactly the requested ``information`` fields + ``country``/``region``/``industry`` as
  sent by the client (``region`` is ``null`` when ``regions`` was empty, Q-E10).
- A cancelled / timed-out / failed run removes its intermediate files (``candidates.jsonl``,
  ``crawl/``, ``result.json``); the manager removes the job directory on deletion.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from leadscraper import constants as C
from leadscraper.crawler.fetcher import Fetcher
from leadscraper.crawler.pages import SiteCrawl, crawl_site
from leadscraper.crawler.robots import RobotsCache
from leadscraper.crawler.website import (
    HostGuard,
    Resolve,
    WebsiteRejected,
    system_resolve,
    website_for,
)
from leadscraper.domain.models import EmailFinding, JobStatus
from leadscraper.domain.quota import allocate
from leadscraper.extractors import address as address_x
from leadscraper.extractors import legal as legal_x
from leadscraper.extractors import objection as objection_x
from leadscraper.extractors import phone as phone_x
from leadscraper.extractors.emails import extract_emails
from leadscraper.extractors.jsonld import JsonLdOrg, extract_organizations
from leadscraper.extractors.scoring import ScoredEmail, rank_emails
from leadscraper.extractors.text import page_text
from leadscraper.jobs.callback import deliver_callback
from leadscraper.jobs.manager import JobManager, JobState
from leadscraper.observability import metrics
from leadscraper.observability.logging import get_logger
from leadscraper.schemas.scrape import InformationField, ScrapeRequest
from leadscraper.services.dedup import DedupEntry, Deduplicator
from leadscraper.services.planner import build_plan, slice_key
from leadscraper.services.resolver.resolve import ResolutionError, ResolvedRequest, Resolver
from leadscraper.services.verify_service import EmailVerifier
from leadscraper.settings import Settings
from leadscraper.sources.osm_overpass import OverpassAdapter, OverpassGate
from leadscraper.sources.registry import build_adapters
from leadscraper.verification.lists import SuppressionList

log = get_logger(__name__)
F = InformationField
VerifierFactory = Callable[[Settings], EmailVerifier]
UNUSABLE_RESULTS = frozenset({"undeliverable", "suppressed"})


@dataclass
class PipelineDeps:
    settings: Settings
    resolver: Resolver
    gate: OverpassGate
    verifier_factory: VerifierFactory
    dns_resolve: Resolve = system_resolve                   # HostGuard (SSRF) lookups
    client_factory: Callable[[], httpx.AsyncClient] = field(default=lambda: httpx.AsyncClient(
        timeout=C.CRAWLER_HTTP_TIMEOUT_S, follow_redirects=False))
    suppression: SuppressionList = field(default_factory=SuppressionList)
    callback_client_factory: Callable[[], httpx.AsyncClient] = field(default=lambda: httpx.AsyncClient(
        timeout=C.CALLBACK_TIMEOUT_S, follow_redirects=False))
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)   # timeout callbacks


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
    for page in ordered:                          # legal notice first (A§1 #6)
        for email in sorted(extract_emails(page.html)):
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


def extract_company(crawl: SiteCrawl, entry: DedupEntry, req: ScrapeRequest,
                    resolved: ResolvedRequest) -> Extracted:
    """Run only the extractors the requested ``information`` needs (A§1 #7)."""
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
        candidates = [x for o in orgs for x in (o.legal_name, o.name) if x] + [entry.candidate.name]
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
    return Extracted(ranked, fields, objection)


def assemble_record(req: ScrapeRequest, entry: DedupEntry, fields: dict[str, Any],
                    email: str | None) -> dict[str, Any]:
    """Only the requested fields (in request order) + the three context labels (A§2.3)."""
    record: dict[str, Any] = {}
    for f in req.information:
        record[f.value] = email if f is F.COMPANY_EMAIL else fields.get(f.value)
    record["country"] = req.country
    record["region"] = entry.region_label or None           # Q-E10: null when regions == []
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
            # Q5 max-runtime: the sweeper marks the job failed/job_timeout *before* cancelling it,
            # so a timed-out job counts as `failed`; a DELETE-cancelled job counts as `cancelled`.
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
            fetcher = Fetcher(self.settings, client, HostGuard(self.deps.dns_resolve))
            RobotsCache(fetcher).attach()
            self.fetcher = fetcher
            self.sem = asyncio.Semaphore(self.settings.crawler_global_concurrency)
            adapters = build_adapters(self.settings, resolved.profile, gate=self.deps.gate,
                                      areas=plan.areas, industries=plan.industries, client=client)
            try:
                await self._discover_all(adapters)
                if self.tasks:
                    await asyncio.gather(*list(self.tasks), return_exceptions=True)
            finally:
                for task in self.tasks:
                    task.cancel()
            for adapter in adapters:
                if isinstance(adapter, OverpassAdapter):
                    self.warnings.extend(w for w in adapter.warnings if w not in self.warnings)
        self._finish()

    async def _discover_all(self, adapters: list) -> None:
        plan = self.plan
        taken: dict[str, int] = {k: 0 for k in plan.slices}
        surplus: dict[str, list[DedupEntry]] = {k: [] for k in plan.slices}
        with self.job.candidates_path.open("a", encoding="utf-8") as out:
            for s in plan.ordered():
                key = slice_key(s)
                self.stats.setdefault(key, SliceStats())
                for adapter in adapters:
                    if self.stop.is_set():
                        return
                    async for cand in adapter.discover(s):
                        out.write(json.dumps({"slice": key, **dataclasses.asdict(cand)},
                                             ensure_ascii=False) + "\n")
                        res = self.dedup.add(cand, s)
                        if not res.new:
                            continue
                        self.progress["candidates"] += 1
                        self.stats[key].candidates += 1
                        if taken[key] < plan.quota(key):
                            taken[key] += 1
                            self._schedule(res.entry)
                        else:
                            surplus[key].append(res.entry)
                        if self.stop.is_set():
                            return
                    out.flush()
                plan.record(key, taken[key])
                self._publish()
        # re-allocate with the real capacities (water-filling, A§3.5) and use the surplus
        capacity = {k: taken[k] + len(surplus[k]) for k in plan.slices}
        alloc = allocate(plan.target, capacity)
        for k, extra in surplus.items():
            for entry in extra[:max(0, alloc[k] - taken[k])]:
                if self.stop.is_set():
                    return
                self._schedule(entry)

    def _schedule(self, entry: DedupEntry) -> None:
        task = asyncio.create_task(self._process(entry))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _process(self, entry: DedupEntry) -> None:
        try:
            await self._process_inner(entry)
        except asyncio.CancelledError:
            raise
        except Exception:                                # one broken site never fails the job
            log.exception("company_processing_failed", source_ref=entry.candidate.source_ref)

    async def _process_inner(self, entry: DedupEntry) -> None:
        async with self.sem:
            if self.stop.is_set():
                return
            try:
                site = website_for(entry.candidate)
            except WebsiteRejected:
                return                                       # no own website → skipped (A§4)
            if site.registered_domain in self.seen_domains:
                return
            self.seen_domains.add(site.registered_domain)
            crawl = await crawl_site(self.fetcher, site, keywords=self.resolved.profile.contact_keywords,
                                     fallback_paths=self.deps.resolver.profiles.fallback_paths,
                                     legal_markers=self.deps.resolver.profiles.contact_pages.legal_markers,
                                     save_dir=self.job.crawl_dir)
            key = slice_key(entry.slice)
            self.progress["crawled"] += 1
            self.stats.setdefault(key, SliceStats()).crawled += 1
            if crawl.skipped or not crawl.pages or self.stop.is_set():
                self._publish()
                return
            final_domain = crawl.website.registered_domain
            if final_domain != site.registered_domain:
                if final_domain in self.seen_domains:        # redirected onto a known company
                    self._publish()
                    return
                self.seen_domains.add(final_domain)
            record = await self._validate_and_assemble(entry, crawl)
            if record is not None and not self.stop.is_set():
                self.accepted.append(Accepted(entry, record))
                if record.get("company_email"):
                    self.progress["with_email"] += 1
                    self.stats.setdefault(key, SliceStats()).with_email += 1
                if len(self.accepted) >= self.req.max_output:
                    self.stop.set()                      # target met → stop early
            self._publish()

    async def _validate_and_assemble(self, entry: DedupEntry, crawl: SiteCrawl) -> dict[str, Any] | None:
        cand = entry.candidate
        region_confidence = "high" if (cand.coords_storable and cand.source == "osm") else "low"
        if region_confidence == "low":                   # A§3.8: excluded by default
            return None
        ex = extract_company(crawl, entry, self.req, self.resolved)
        if ex.objection:
            log.info("marketing_objection", domain=crawl.website.registered_domain)
            return None
        email: str | None = None
        if F.COMPANY_EMAIL in self.req.information:
            email = await self._choose_email(ex.ranked)
            if email is None:
                return None                              # not counted toward max_output
        return assemble_record(self.req, entry, ex.fields, email)

    async def _choose_email(self, ranked: list[ScoredEmail]) -> str | None:
        for scored in ranked:
            address = scored.email
            if self.deps.suppression.is_suppressed(address):
                continue
            if self.verifier is not None:
                result = await self.verifier.verify(address, smtp_check=False)
                if result.result.value in UNUSABLE_RESULTS:
                    continue                             # Q15: fall back to the next-best email
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
    """``JobManager.start(job, runner_for(deps, jobs))``: pipeline, then optional callback (T22)."""
    async def run(job: JobState) -> None:
        try:
            await ScrapePipeline(deps, jobs, job).run()
        except asyncio.CancelledError:
            # Q5 timeout (sweeper: failed/job_timeout, then cancel) → still call back once (Q4).
            # A DELETE-cancelled job is not FAILED (and is being removed) → no callback.
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
