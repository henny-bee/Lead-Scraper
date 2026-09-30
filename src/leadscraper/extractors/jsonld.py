"""Schema.org JSON-LD reader: ``Organization`` / ``LocalBusiness`` blocks carry ``email``,
``telephone``, ``legalName`` and ``address`` in the same structure in every country."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from selectolax.parser import HTMLParser

from leadscraper import constants as C

_WRAPPER = re.compile(r"^\s*(?:<!--|<!\[CDATA\[)|(?:-->|\]\]>)\s*$")


@dataclass(slots=True)
class JsonLdOrg:
    types: tuple[str, ...]
    name: str | None = None
    legal_name: str | None = None
    emails: list[str] = field(default_factory=list)
    telephones: list[str] = field(default_factory=list)
    url: str | None = None
    vat_id: str | None = None
    tax_id: str | None = None
    street: str | None = None
    postal_code: str | None = None
    locality: str | None = None
    region: str | None = None
    country: str | None = None
    address_text: str | None = None


def _types(obj: dict[str, Any]) -> tuple[str, ...]:
    raw = obj.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    out = []
    for v in values:
        if isinstance(v, str) and v:
            out.append(v.rsplit("/", 1)[-1].rsplit(":", 1)[-1])   # "schema:Organization", URLs
    return tuple(out)


def is_org_type(types: tuple[str, ...]) -> bool:
    return any(t in C.JSONLD_ORG_TYPES or t.endswith(("Business", "Organization")) for t in types)


def _strs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strs(v)]
    if isinstance(value, dict):
        return _strs(value.get("@value") or value.get("value") or value.get("name"))
    text = str(value).strip()
    return [text] if text else []


def _first(value: Any) -> str | None:
    items = _strs(value)
    return items[0] if items else None


def _address(org: JsonLdOrg, value: Any) -> None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        org.address_text = value.strip() or None
        return
    if not isinstance(value, dict):
        return
    org.street = _first(value.get("streetAddress"))
    org.postal_code = _first(value.get("postalCode"))
    org.locality = _first(value.get("addressLocality"))
    org.region = _first(value.get("addressRegion"))
    country = value.get("addressCountry")
    org.country = _first(country.get("name") if isinstance(country, dict) else country)
    parts = [p for p in (org.street, " ".join(x for x in (org.postal_code, org.locality) if x),
                         org.country) if p]
    org.address_text = ", ".join(parts) or None


def _to_org(obj: dict[str, Any], types: tuple[str, ...]) -> JsonLdOrg:
    org = JsonLdOrg(types=types, name=_first(obj.get("name")), legal_name=_first(obj.get("legalName")),
                    url=_first(obj.get("url")), vat_id=_first(obj.get("vatID")),
                    tax_id=_first(obj.get("taxID")))
    emails = _strs(obj.get("email"))
    telephones = _strs(obj.get("telephone"))
    points = obj.get("contactPoint")
    for cp in points if isinstance(points, list) else [points]:
        if isinstance(cp, dict):
            emails += _strs(cp.get("email"))
            telephones += _strs(cp.get("telephone"))
    org.emails = list(dict.fromkeys(e.removeprefix("mailto:").strip().lower() for e in emails if e))
    org.telephones = list(dict.fromkeys(t.removeprefix("tel:").strip() for t in telephones if t))
    _address(org, obj.get("address"))
    return org


def _walk(node: Any, out: list[JsonLdOrg], depth: int) -> None:
    if depth > C.JSONLD_MAX_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    types = _types(node)
    if is_org_type(types):
        out.append(_to_org(node, types))
    for key, value in node.items():
        if key in ("@context", "address", "contactPoint"):
            continue
        if isinstance(value, (dict, list)):
            _walk(value, out, depth + 1)


def parse_blocks(page_html: str) -> list[Any]:
    blocks: list[Any] = []
    for node in HTMLParser(page_html).css('script[type="application/ld+json"]')[:C.JSONLD_MAX_BLOCKS]:
        text = _WRAPPER.sub("", node.text(deep=True) or "").strip()
        if not text:
            continue
        try:
            blocks.append(json.loads(text))
        except ValueError:
            try:
                blocks.append(json.loads(text, strict=False))      # raw newlines in strings
            except ValueError:
                continue
    return blocks


def extract_organizations(page_html: str) -> list[JsonLdOrg]:
    out: list[JsonLdOrg] = []
    for block in parse_blocks(page_html):
        _walk(block, out, 0)
    return out
