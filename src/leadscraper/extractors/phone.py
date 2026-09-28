"""Phone extraction (ARCHITECTURE.md §3.6): ``phonenumbers`` with region = country code → E.164.

Only runs when ``phone`` is requested (A§1 #7). Structured sources (JSON-LD ``telephone``, OSM
``phone`` hint) are tried first, then page text; numbers labelled as fax are skipped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import phonenumbers

_FAX = re.compile(r"(fax|telefax|télécopie|telecopie)\s*[.:]?\s*$", re.I)
_LOOKBEHIND = 24


def to_e164(raw: str, region: str) -> str | None:
    try:
        num = phonenumbers.parse(raw, region.upper())
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def phones_in_text(text: str, region: str) -> list[str]:
    out: list[str] = []
    for match in phonenumbers.PhoneNumberMatcher(text, region.upper()):
        before = text[max(0, match.start - _LOOKBEHIND):match.start]
        if _FAX.search(before):
            continue
        if not phonenumbers.is_valid_number(match.number):
            continue
        e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
        if e164 not in out:
            out.append(e164)
    return out


def extract_phone(texts: Iterable[str], region: str, structured: Iterable[str] = ()) -> str | None:
    """First valid number: structured values first, then page texts in the given order."""
    for raw in structured:
        e164 = to_e164(raw, region)
        if e164:
            return e164
    for text in texts:
        found = phones_in_text(text, region)
        if found:
            return found[0]
    return None
