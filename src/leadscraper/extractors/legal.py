"""Legal name & form, register number, VAT id (ARCHITECTURE.md §3.6 table; T16).

- Legal forms per country from ``config/i18n/legal_forms.yaml``: suffix forms (``GmbH``, ``SAS``,
  ``Ltd``, ``B.V.``) and prefix forms (a form placed before the name, supported for countries whose YAML entry defines ``prefix``).
- Register numbers (PLAN Q-E12): per-country ``register_pattern`` from the same file, validated
  with the configured python-stdnum module (``de.handelsregisternummer``, ``fr.siren``,
  ``no.orgnr``). DE: when stdnum confirms court + number the normalised form is returned
  (``München HRB 123456``); otherwise the raw value is returned **only** because it matched the
  strict pattern ``HRA|HRB|GnR|PR|VR`` + digits (Handelsregister numbers have no checksum, stdnum
  mostly checks the court name). FR SIREN / NO orgnr failing the checksum → ``None``.
- VAT ids: EU-style prefixed numbers validated with ``stdnum.eu.vat``; otherwise a VAT keyword
  (USt-IdNr, VAT, TVA, IVA, BTW, …) followed by a number validated with the country's
  stdnum ``vat`` module. Only format validity is checked, never existence (A§13).
Pure functions, no I/O except reading the YAML once.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz
from stdnum.eu import vat as eu_vat
from stdnum.exceptions import ValidationError
from stdnum.util import get_cc_module

from leadscraper import constants as C
from leadscraper.services.resolver.geo import norm

LEGAL_FORMS_FILE = C.CONFIG_DIR / "i18n" / "legal_forms.yaml"
MAX_NAME_WORDS = C.LEGAL_NAME_MAX_WORDS
MAX_LINE = C.LEGAL_NAME_MAX_LINE
NAME_MATCH_MIN = C.LEGAL_NAME_SOURCE_MATCH_MIN
EU_VAT_PREFIXES = ("AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR",
                   "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI",
                   "SK", "XI")
_EU_VAT_RE = re.compile(rf"\b({'|'.join(EU_VAT_PREFIXES)})[ \-]?([0-9A-Z][0-9A-Z .\-]{{6,14}}[0-9A-Z])\b")
_VAT_KEYWORDS = re.compile(
    r"(ust[\s.\-]*id[\s.\-]*(nr|nummer)?|umsatzsteuer[\w\s\-]*?(nummer|id)|\bUID\b|\bvat\b|"
    r"\btva\b|\biva\b|partita iva|\bbtw\b|\bnif\b|\bcif\b|\bmva\b|\bmwst\b|\bgst\b|"
    r"\babn\b|\bcnpj\b)[^0-9A-Z]{0,20}", re.I)
_NUMBER_TOKEN = re.compile(r"[A-Z]{0,3}[ .\-]?\d[0-9A-Z .\-/]{5,24}")
_COURT_RE = re.compile(r"(?:Amtsgericht|Registergericht|AG)\s+([A-ZÄÖÜ][\wäöüß.\-]*(?:\s[A-ZÄÖÜ(][\wäöüß.\-)]*){0,3})")


@dataclass(slots=True, frozen=True)
class LegalForms:
    cc: str
    suffix: tuple[str, ...] = ()
    prefix: tuple[str, ...] = ()
    register_module: str | None = None
    register_pattern: re.Pattern | None = None


@dataclass(slots=True, frozen=True)
class LegalName:
    legal_name: str
    legal_form: str


@lru_cache(maxsize=1)
def _forms_file(path: str = str(LEGAL_FORMS_FILE)) -> dict[str, Any]:
    p = Path(path)
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.is_file() else {}


@lru_cache(maxsize=256)
def legal_forms(cc: str) -> LegalForms:
    entry = _forms_file().get(cc.upper()) or {}
    pattern = entry.get("register_pattern")
    return LegalForms(cc=cc.upper(),
                      suffix=tuple(sorted(map(str, entry.get("suffix") or []), key=len, reverse=True)),
                      prefix=tuple(sorted(map(str, entry.get("prefix") or []), key=len, reverse=True)),
                      register_module=entry.get("register"),
                      register_pattern=re.compile(pattern) if pattern else None)


def _form_alt(forms: Iterable[str]) -> str:
    variants: list[str] = []
    for f in forms:
        for v in (f, f.upper()):
            esc = re.escape(v).replace(r"\ ", r"\s+")
            if esc not in variants:
                variants.append(esc)
    return "|".join(variants)


@lru_cache(maxsize=256)
def _name_regexes(cc: str) -> tuple[re.Pattern | None, re.Pattern | None]:
    forms = legal_forms(cc)
    lead = r"^(?:[^:\n]{0,40}:\s*)?"
    suffix = prefix = None
    if forms.suffix:
        suffix = re.compile(lead + rf"(?P<name>[^\W_][^\n]{{0,100}}?)\s*,?\s+(?P<form>{_form_alt(forms.suffix)})"
                            r"(?=$|[\s,;)(]|\.(?:\s|$))")
    if forms.prefix:
        tail = _form_alt(forms.suffix) if forms.suffix else r"(?!x)x"
        prefix = re.compile(lead + rf"(?P<form>{_form_alt(forms.prefix)})\.?\s+"
                            rf"(?P<name>[^\W\d_][^\n,;(]{{1,80}}?)(?:\s+(?P<tail>{tail}))?\s*(?=$|[,;(])")
    return suffix, prefix


def parse_legal_name(text: str, cc: str) -> LegalName | None:
    """Recognise ``<name> <suffix form>`` or ``<prefix form> <name> [suffix]`` in one line/name."""
    suffix_re, prefix_re = _name_regexes(cc)
    line = " ".join(text.split())
    if not line or len(line) > MAX_LINE:
        return None
    if prefix_re and (m := prefix_re.search(line)):
        name = m.group("name").strip()
        if len(name.split()) <= MAX_NAME_WORDS:
            full = f"{m.group('form')} {name}" + (f" {m.group('tail')}" if m.group("tail") else "")
            return LegalName(full, _canonical_form(m.group("form"), legal_forms(cc).prefix))
    if suffix_re and (m := suffix_re.search(line)):
        name = m.group("name").strip(" ,")
        if 1 <= len(name.split()) <= MAX_NAME_WORDS:
            form = _canonical_form(m.group("form"), legal_forms(cc).suffix)
            return LegalName(f"{name} {' '.join(m.group('form').split())}", form)
    return None


def _canonical_form(found: str, forms: Sequence[str]) -> str:
    key = " ".join(found.split()).casefold()
    return next((f for f in forms if f.casefold() == key), " ".join(found.split()))


def extract_legal_name(texts: Iterable[str], cc: str, *, candidates: Iterable[str] = (),
                       source_name: str | None = None) -> LegalName | None:
    """Best legal name: structured candidates (JSON-LD legalName/name, source name) and page lines.
    Prefers the match most similar to the source name, else the first match (legal page first)."""
    found: list[LegalName] = []
    for value in candidates:
        if value and (hit := parse_legal_name(value, cc)):
            found.append(hit)
    for text in texts:
        for line in text.splitlines():
            if hit := parse_legal_name(line, cc):
                found.append(hit)
    if not found:
        return None
    if source_name:
        scored = [(fuzz.token_set_ratio(norm(source_name), norm(h.legal_name)), -i, h)
                  for i, h in enumerate(found)]
        best = max(scored)
        if best[0] >= NAME_MATCH_MIN:
            return best[2]
    return found[0]


# --- register number --------------------------------------------------------------------------------
def _stdnum(module: str):
    try:
        return importlib.import_module(f"stdnum.{module}")
    except ImportError:
        return None


def extract_register_number(text: str, cc: str) -> str | None:
    forms = legal_forms(cc)
    if forms.register_pattern is None:
        return None
    module = _stdnum(forms.register_module) if forms.register_module else None
    flat = " ".join(text.split())
    for m in forms.register_pattern.finditer(flat):
        if cc.upper() == "DE":
            kind, number = m.group(1), " ".join(m.group(2).split())
            raw = f"{kind} {number}"
            courts = list(_COURT_RE.finditer(flat[max(0, m.start() - 120):m.start()]))
            court = courts[-1] if courts else None       # nearest court name before the number
            if module is not None and court:
                try:
                    return module.validate(f"{court.group(1).strip(' ,.')} {raw}")
                except ValidationError:
                    pass
            return raw
        number = m.group(1)
        if module is None:
            return " ".join(number.split())
        try:
            return module.compact(module.validate(number))
        except ValidationError:
            continue                            # checksum failed → not a register number
    return None


# --- VAT id --------------------------------------------------------------------------------------------
def extract_vat_id(text: str, cc: str) -> str | None:
    flat = " ".join(text.split())
    for m in _EU_VAT_RE.finditer(flat):
        candidate = m.group(1) + re.sub(r"[ .\-]", "", m.group(2))
        for end in range(len(candidate), 9, -1):          # trailing words may have been captured
            try:
                return eu_vat.compact(eu_vat.validate(candidate[:end]))
            except ValidationError:
                continue
    module = get_cc_module(cc.lower(), "vat")
    if module is None:
        return None
    for kw in _VAT_KEYWORDS.finditer(flat):
        window = flat[kw.end():kw.end() + 40]
        token = _NUMBER_TOKEN.match(window)
        if not token:
            continue
        raw = token.group(0).strip()
        for end in range(len(raw), 5, -1):
            try:
                value = module.validate(raw[:end])
            except ValidationError:
                continue
            return module.compact(value)
    return None
