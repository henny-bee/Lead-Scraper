"""Company-identity check for looked-up websites."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from leadscraper import constants as C
from leadscraper.extractors.legal import parse_legal_name
from leadscraper.services.dedup import normalize_name
from leadscraper.services.resolver.geo import norm

_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
                           "ß": "ss", "ẞ": "SS"})
_NON_WORD = re.compile(r"[^\w\s]")


@dataclass(slots=True, frozen=True)
class IdentityMatch:
    ok: bool
    name_score: float
    location_hit: bool
    reasons: tuple[str, ...] = ()


def translit(text: str) -> str:
    """``Förde Straße`` → ``foerde strasse`` (German transliteration, then accents stripped)."""
    return _NON_WORD.sub(" ", norm(text.translate(_TRANSLIT)))


def company_tokens(name: str, legal_form_tokens: Sequence[tuple[str, ...]]) -> list[str]:
    """The normalised, transliterated name without legal forms."""
    return normalize_name(translit(name), list(legal_form_tokens)).split()


def _generic(values: Iterable[str | None]) -> set[str]:
    return {tok for v in values if v for tok in translit(v).split()}


def _location_hit(texts: Sequence[str], postal_code: str | None, city: str | None) -> bool:
    if postal_code and postal_code.split():
        # whole token (28195 ≠ 128195); inner spaces optional ("SW1A 1AA" = "SW1A1AA")
        code = re.compile(r"(?<!\w)" + r"\s*".join(map(re.escape, postal_code.split())) + r"(?!\w)",
                          re.IGNORECASE)
        if any(code.search(t) for t in texts):
            return True
    if city:
        wanted = translit(city)
        pattern = re.compile(rf"(?<!\w){re.escape(wanted)}(?!\w)")
        return bool(wanted) and any(pattern.search(translit(t)) for t in texts)
    return False


def _norm_postcode(code: str) -> str:
    return re.sub(r"\s+", "", code).upper()


def _own_location(own: tuple[str | None, str | None], postal_code: str | None, city: str | None,
                  area_name: str | None) -> tuple[bool, bool]:
    """``(location_hit, postcode_mismatch)`` from the site's own address."""
    own_postcode, own_city = own
    if own_postcode and postal_code:
        same = _norm_postcode(own_postcode) == _norm_postcode(postal_code)
        return same, not same
    wanted = translit(city or area_name or "")
    if own_city and wanted:
        return re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", translit(own_city)) is not None, False
    return False, False


def matches_company(texts: Sequence[str], *, name: str, postal_code: str | None, city: str | None,
                    area_name: str | None, country_code: str,
                    legal_form_tokens: Sequence[tuple[str, ...]], generic_tokens: Iterable[str],
                    titles: Sequence[str] = (), require_location: bool = False,
                    own_address: tuple[str | None, str | None] | None = None,
                    strict_location: bool = False) -> IdentityMatch:
    """Does this website belong to the candidate company?"""
    full = company_tokens(name, legal_form_tokens)
    stop = _generic([city, area_name]) | _generic(generic_tokens)
    distinct = [t for t in full if t not in stop]
    lines = [line for t in texts for line in t.splitlines() if line.strip()]
    legal_hits = [h.legal_name for line in lines if (h := parse_legal_name(line, country_code))]
    if strict_location and own_address is not None:
        location, mismatch = _own_location(own_address, postal_code, city, area_name)
        if mismatch:
            return IdentityMatch(False, 0.0, False, ("postcode_mismatch",))
    else:
        location = _location_hit(texts, postal_code, city)
    if not any(len(t) >= 3 for t in distinct):                    # generic name only
        target = " ".join(full)
        best = max((fuzz.ratio(target, " ".join(company_tokens(h, legal_form_tokens)))
                    for h in legal_hits), default=0.0)
        ok = bool(target) and best >= C.IDENTITY_NAME_ONLY_MIN_SCORE and location
        return IdentityMatch(ok, best, location, () if ok else ("generic_name",))
    # score the full name (legal forms stripped) against candidates normalised
    # the same way, and only count a candidate that contains every distinct token — otherwise one
    # shared surname ("Müller") would match another company's Impressum.
    query = " ".join(full)
    required = {t for t in distinct if len(t) >= 3}
    score = 0.0
    for candidate in (*titles, *legal_hits, *lines):
        tokens = company_tokens(candidate, legal_form_tokens)
        if required <= set(tokens):
            score = max(score, fuzz.token_set_ratio(query, " ".join(tokens)))
    reasons: list[str] = []
    if score < C.IDENTITY_NAME_MIN_SCORE:
        reasons.append("name")
    if location:
        ok = score >= C.IDENTITY_NAME_MIN_SCORE
    elif postal_code is None and city is None and not require_location:
        ok = score >= C.IDENTITY_NAME_ONLY_MIN_SCORE
        if not ok:
            reasons.append("name_only")
    else:
        ok = False
        reasons.append("location")
    return IdentityMatch(ok, score, location, tuple(reasons))
