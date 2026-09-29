"""Address & postal code."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from leadscraper import constants as C

GEONAMES_POSTAL_DIR = C.DATA_DIR / "geonames" / "postal"
#: Lines that are phone numbers or identifiers, never an address line.
_NOT_ADDRESS = re.compile(
    r"(\btel\b|telefon|phone|fax|mobil|\+\d|whatsapp|nummer|number|\bhr[ab]\b|"
    r"\bust\b|ust-?id|\brcs\b|siren|siret|iban|\bbic\b|\bblz\b|konto|\bvat\b|\btva\b)",
    re.I)
_HAS_WORD = re.compile(r"[^\W\d_]{2,}")
MAX_LINE = C.ADDRESS_MAX_LINE


@dataclass(slots=True, frozen=True)
class AddressResult:
    address: str
    postal_code: str


def _search_patterns(patterns: Sequence[re.Pattern]) -> list[re.Pattern]:
    return [re.compile(rf"(?<![\w-])(?:{p.pattern})(?![\w-])", re.I) for p in patterns]


@lru_cache(maxsize=64)
def geonames_postal_codes(cc: str, directory: str = str(GEONAMES_POSTAL_DIR)) -> frozenset[str] | None:
    path = Path(directory) / f"{cc.upper()}.txt"
    if not path.is_file():
        return None
    codes = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and parts[0].upper() == cc.upper():
            codes.add(parts[1].replace(" ", "").upper())
    return frozenset(codes)


def _street_like(line: str) -> bool:
    return (len(line) <= MAX_LINE and bool(re.search(r"\d", line)) and bool(_HAS_WORD.search(line))
            and not _NOT_ADDRESS.search(line))


def extract_address(text: str, patterns: Sequence[re.Pattern], cc: str,
                    known_codes: frozenset[str] | None = None) -> AddressResult | None:
    """First plausible ``(address, postal_code)`` in ``text`` (lines as produced by page_text)."""
    if not patterns:
        return None
    known = known_codes if known_codes is not None else geonames_postal_codes(cc)
    regexes = _search_patterns(patterns)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE or _NOT_ADDRESS.search(line):
            continue
        for rx in regexes:
            m = rx.search(line)
            if not m:
                continue
            rest = (line[:m.start()] + " " + line[m.end():]).strip()
            if not _HAS_WORD.search(rest):              # postal code needs a city next to it
                continue
            code = m.group(0).strip()
            if known is not None and code.replace(" ", "").upper() not in known:
                continue
            parts = [line.strip(" ,")]
            if i > 0 and _street_like(lines[i - 1]) and not any(r.search(lines[i - 1]) for r in regexes):
                parts.insert(0, lines[i - 1].strip(" ,"))
            return AddressResult(", ".join(parts), code)
    return None


# --- the company's own address ---------------------------------------------------------------------
def _city_after(address: str, postal_code: str) -> str | None:
    """``"Hafenstraße 1, 28195 Bremen"`` → ``"Bremen"`` (the words after the postcode, else before)."""
    i = address.find(postal_code)
    if i < 0:
        return None
    after = address[i + len(postal_code):].strip(" ,;-").split(",")[0].strip()
    if _HAS_WORD.search(after):
        return after
    before = address[:i].strip(" ,;-").split(",")[-1].strip()
    return before if _HAS_WORD.search(before) and not re.search(r"\d", before) else None


def own_address(legal_page_texts: Sequence[str], jsonld_orgs: Sequence[object],
                patterns: Sequence[re.Pattern], cc: str) -> tuple[str | None, str | None] | None:
    """The company's own ``(postcode, city)``: the first:func:`extract_address` hit on the legal
    pages (in crawl order), else the JSON-LD ``Organization`` ``postal_code``/``locality``;
    ``None`` when neither exists."""
    for text in legal_page_texts:
        hit = extract_address(text, patterns, cc)
        if hit is not None:
            return hit.postal_code, _city_after(hit.address, hit.postal_code)
    for org in jsonld_orgs:
        postcode, city = getattr(org, "postal_code", None), getattr(org, "locality", None)
        if postcode or city:
            return postcode, city
    return None

