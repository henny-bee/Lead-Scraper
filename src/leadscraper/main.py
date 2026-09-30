"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from leadscraper import __version__
from leadscraper.api.deps import RateLimiter, rate_limit, require_api_key
from leadscraper.api.errors import install_error_handlers
from leadscraper.api.routes import health, meta, scrape, verify, website
from leadscraper.jobs.cleanup import CleanupSweeper
from leadscraper.jobs.manager import JobManager
from leadscraper.observability.logging import configure_logging, get_logger
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.services.scrape_service import PipelineDeps
from leadscraper.services.verify_service import build_verifier
from leadscraper.settings import Settings, get_settings
from leadscraper.sources.osm_overpass import OverpassGatePool
from leadscraper.sources.web_search import SearchGate, build_search_backend

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.app_env)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        jobs: JobManager = app.state.jobs
        purged = jobs.purge_stale_temp()          # restart = jobs lost
        log.info("startup", temp_dir=str(jobs.temp_root), purged_stale_dirs=purged)
        names = await asyncio.to_thread(geo.warm_up)     # CLDR country index
        app.state.resolver_ready = True
        log.info("resolver_warmed", country_names=names)
        sweeper = CleanupSweeper(jobs, settings)
        app.state.sweeper = sweeper
        sweeper.start()
        try:
            yield
        finally:
            await sweeper.stop()
            for task in list(app.state.pipeline_deps.background_tasks):
                task.cancel()                     # pending timeout callbacks
            await jobs.close()                    # cancel tasks, delete all jobs + temp dirs

    app = FastAPI(title="Company Lead Scraper API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.jobs = JobManager(settings.temp_dir)
    app.state.resolver_ready = False
    app.state.rate_limiter = RateLimiter()         # in-process, per IP, bounded
    app.state.resolver = Resolver(settings)       # local data only; Nominatim iff NOMINATIM_URL
    app.state.verifier_factory = build_verifier  # replaced in tests (fake DNS / SMTP)
    # process-wide Overpass politeness, one gate per endpoint
    app.state.overpass_gate = OverpassGatePool.for_settings(settings)
    # process-wide search gate; backend from WEB_SEARCH_URL (None when "off").
    app.state.search_gate = SearchGate()
    app.state.pipeline_deps = PipelineDeps(
        settings=settings, resolver=app.state.resolver, gate=app.state.overpass_gate,
        verifier_factory=lambda s: app.state.verifier_factory(s),
        search_backend=build_search_backend(settings), search_gate=app.state.search_gate)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(health.metrics_router, dependencies=[Depends(require_api_key)])
    protected = [Depends(rate_limit), Depends(require_api_key)]   # /health stays open
    app.include_router(scrape.router, dependencies=protected)
    app.include_router(meta.router, dependencies=protected)
    app.include_router(verify.router, dependencies=protected)
    app.include_router(website.router, dependencies=protected)
    return app


app = create_app()
