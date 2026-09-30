"""``GET /contacts`` and ``POST /websites/find``"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from leadscraper.services.website_service import find_website, website_contacts

router = APIRouter(tags=["website"])


class FindWebsiteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(min_length=1, max_length=200, examples=["Spedition Müller GmbH"])


@router.get("/contacts")
async def contacts(request: Request,
                   website: str = Query(min_length=1, max_length=2048, examples=["vercel.com"]),
                   mode: Literal["homepage", "key_pages", "deep"] = "key_pages",
                   country: str | None = Query(default=None, min_length=2, max_length=100)
                   ) -> dict[str, Any]:
    return await website_contacts(request.app.state.pipeline_deps, request.app.state.resolver,
                                  website.strip(), mode=mode, country=country)


@router.post("/websites/find")
async def websites_find(req: FindWebsiteRequest, request: Request) -> dict[str, Any]:
    return await find_website(request.app.state.pipeline_deps, request.app.state.resolver,
                              req.name.strip(), dict(req.model_extra or {}))
