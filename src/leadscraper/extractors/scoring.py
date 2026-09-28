"""Best-email selection (ARCHITECTURE.md §3.7).

All found addresses are kept per job with their ``source_url`` (``EmailFinding``); one
``company_email`` is chosen by score. Weights, generic/special local parts and the per-country
free-mail weight come from ``config/i18n/role_emails.yaml`` — no weights in code:

- same registered domain as the website ............................ ``same_domain`` (+50)
- generic local part of the country's languages (config order) ...... ``generic_max``…``generic_min`` (+30…+20)
- found on the legal-notice page or in JSON-LD Organization ........ ``legal_or_jsonld`` (+10)
- looks like ``firstname.lastname`` ................................. ``person_like`` (−10)
- free-mail provider ................................................ ``free_mail`` (−20, per-country override)
- special-function address (privacy/jobs/press/noreply/webmaster) .. ``special_function`` (−40)

Ties keep the order in which the addresses were found. :func:`rank_emails` returns the full
ranking so a verification step can fall back to the next-best address (Q15).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from leadscraper import constants as C
from leadscraper.crawler.website import registered_domain
from leadscraper.domain.models import EmailFinding

ROLE_EMAILS_FILE = C.CONFIG_DIR / "i18n" / "role_emails.yaml"
FREE_MAIL_FILE = Path(__file__).resolve().parents[1] / "verification" / "data" / "free_email_domains.txt"
_SPLIT = re.compile(r"[._+\-]")
_PERSON = re.compile(r"^[a-z]{1,20}[._\-][a-z]{2,30}$")


@dataclass(slots=True, frozen=True)
class ScoringConfig:
    weights: dict[str, float]
    generic: dict[str, tuple[str, ...]]
    special: frozenset[str]
    free_mail_by_country: dict[str, float]
    free_mail_fallback: frozenset[str]


@dataclass(slots=True)
class ScoredEmail:
    finding: EmailFinding
    score: float
    reasons: dict[str, float] = field(default_factory=dict)

    @property
    def email(self) -> str:
        return self.finding.email


def load_scoring_config(path: Path | str = ROLE_EMAILS_FILE) -> ScoringConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ScoringConfig(
        weights={str(k): float(v) for k, v in (raw.get("weights") or {}).items()},
        generic={str(lang): tuple(str(x).lower() for x in words or [])
                 for lang, words in (raw.get("generic") or {}).items()},
        special=frozenset(str(x).lower() for x in raw.get("special_function") or []),
        free_mail_by_country={str(k).upper(): float(v)
                              for k, v in (raw.get("free_mail_weight_by_country") or {}).items()},
        free_mail_fallback=frozenset(str(x).lower() for x in raw.get("free_mail_fallback") or []),
    )


@lru_cache(maxsize=1)
def default_config() -> ScoringConfig:
    return load_scoring_config()


@lru_cache(maxsize=1)
def _vendored_free_mail() -> frozenset[str] | None:
    if not FREE_MAIL_FILE.is_file():
        return None
    lines = FREE_MAIL_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(x.strip().lower() for x in lines if x.strip() and not x.startswith("#"))


def default_free_mail_predicate(config: ScoringConfig) -> Callable[[str], bool]:
    domains = _vendored_free_mail() or config.free_mail_fallback
    return lambda domain: domain.lower() in domains


def generic_order(config: ScoringConfig, languages: Sequence[str]) -> list[str]:
    order: list[str] = []
    for lang in (*languages, "en"):
        for word in config.generic.get(lang, ()):
            if word not in order:
                order.append(word)
    return order


def _first_word(local: str) -> str:
    return _SPLIT.split(local, maxsplit=1)[0]


def score_email(finding: EmailFinding, *, website_domain: str | None, languages: Sequence[str],
                country: str, config: ScoringConfig | None = None,
                is_free: Callable[[str], bool] | None = None) -> ScoredEmail:
    cfg = config or default_config()
    free = is_free or default_free_mail_predicate(cfg)
    w = cfg.weights
    local, _, domain = finding.email.lower().rpartition("@")
    reasons: dict[str, float] = {}
    if website_domain and registered_domain(domain) == website_domain:
        reasons["same_domain"] = w.get("same_domain", 0)
    order = generic_order(cfg, languages)
    head = _first_word(local)
    special = local in cfg.special or head in cfg.special
    if not special:
        key = local if local in order else head if head in order else None
        if key is not None:
            hi, lo = w.get("generic_max", 0), w.get("generic_min", 0)
            step = (hi - lo) / (len(order) - 1) if len(order) > 1 else 0
            reasons["generic"] = round(hi - step * order.index(key), 2)
        elif _PERSON.match(local):
            reasons["person_like"] = w.get("person_like", 0)
    else:
        reasons["special_function"] = w.get("special_function", 0)
    if finding.on_legal_notice or finding.from_jsonld:
        reasons["legal_or_jsonld"] = w.get("legal_or_jsonld", 0)
    if free(domain):
        reasons["free_mail"] = cfg.free_mail_by_country.get(country.upper(), w.get("free_mail", 0))
    return ScoredEmail(finding, sum(reasons.values()), reasons)


def rank_emails(findings: Iterable[EmailFinding], *, website: str | None, languages: Sequence[str],
                country: str, config: ScoringConfig | None = None,
                is_free: Callable[[str], bool] | None = None) -> list[ScoredEmail]:
    """All distinct addresses, best first (stable for ties). Provenance flags are OR-ed."""
    merged: dict[str, EmailFinding] = {}
    for f in findings:
        key = f.email.lower()
        if key in merged:
            m = merged[key]
            m.on_legal_notice |= f.on_legal_notice
            m.from_jsonld |= f.from_jsonld
        else:
            merged[key] = EmailFinding(key, f.source_url, f.on_legal_notice, f.from_jsonld)
    site_domain = registered_domain(website) if website else None
    scored = [score_email(f, website_domain=site_domain, languages=languages, country=country,
                          config=config, is_free=is_free) for f in merged.values()]
    return sorted(scored, key=lambda s: -s.score)          # sorted() is stable → found order on ties


def select_best_email(findings: Iterable[EmailFinding], **kwargs: Any) -> ScoredEmail | None:
    ranked = rank_emails(findings, **kwargs)
    return ranked[0] if ranked else None
