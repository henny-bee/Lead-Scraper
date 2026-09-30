"""Autocomplete endpoints: ``GET /meta/countries``, ``GET /meta/regions?country=DE``, ``GET
/meta/industries?q=logistik``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from leadscraper import constants as C
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.resolve import Resolver

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/countries")
async def countries() -> dict[str, Any]:
    items = geo.list_countries()
    return {"status": "success", "count": len(items), "countries": items}


@router.get("/regions")
async def regions(request: Request,
                  country: str = Query(min_length=2, max_length=100)) -> dict[str, Any]:
    resolver: Resolver = request.app.state.resolver
    cc = resolver.resolve_country(country).code          # free text accepted, 422 if unknown
    items = geo.list_regions(cc)
    return {"status": "success", "country": cc, "count": len(items), "regions": items}


@router.get("/industries")
async def industries(request: Request, q: str = Query(default="", max_length=100)) -> dict[str, Any]:
    resolver: Resolver = request.app.state.resolver
    items = resolver.catalog.search(q, limit=C.META_INDUSTRY_LIMIT)
    return {"status": "success", "count": len(items), "industries": items,
            "scheme": resolver.catalog.scheme, "version": resolver.catalog.version}
