"""FastAPI application factory (ARCHITECTURE.md §2, §6). Run with a single Uvicorn worker (C15).

Startup performs no outbound network calls (zero-config, C1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from leadscraper import __version__
from leadscraper.api.deps import RateLimiter, rate_limit, require_api_key
from leadscraper.api.errors import install_error_handlers
from leadscraper.api.routes import health, meta, scrape, verify
from leadscraper.jobs.cleanup import CleanupSweeper
from leadscraper.jobs.manager import JobManager
from leadscraper.observability.logging import configure_logging, get_logger
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.services.scrape_service import PipelineDeps
from leadscraper.services.verify_service import build_verifier
from leadscraper.settings import Settings, get_settings
from leadscraper.sources.osm_overpass import OverpassGate

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.app_env)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        jobs: JobManager = app.state.jobs
        purged = jobs.purge_stale_temp()          # restart = jobs lost (A§8 trade-off)
        log.info("startup", temp_dir=str(jobs.temp_root), purged_stale_dirs=purged)
        names = await asyncio.to_thread(geo.warm_up)     # CLDR country index (~36k names, A§3.2)
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
                task.cancel()                     # pending timeout callbacks (T22)
            await jobs.close()                    # cancel tasks, delete all jobs + temp dirs

    app = FastAPI(title="Company Lead Scraper API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.jobs = JobManager(settings.temp_dir)
    app.state.resolver_ready = False
    app.state.rate_limiter = RateLimiter()         # in-process, per IP, bounded (T23)
    app.state.resolver = Resolver(settings)       # local data only; Nominatim iff NOMINATIM_URL
    app.state.verifier_factory = build_verifier  # replaced in tests (fake DNS / SMTP)
    app.state.overpass_gate = OverpassGate()     # process-wide Overpass politeness (T11)
    app.state.pipeline_deps = PipelineDeps(
        settings=settings, resolver=app.state.resolver, gate=app.state.overpass_gate,
        verifier_factory=lambda s: app.state.verifier_factory(s))
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(health.metrics_router, dependencies=[Depends(require_api_key)])
    protected = [Depends(rate_limit), Depends(require_api_key)]   # /health stays open (Q6)
    app.include_router(scrape.router, dependencies=protected)
    app.include_router(meta.router, dependencies=protected)
    app.include_router(verify.router, dependencies=protected)
    return app


app = create_app()
