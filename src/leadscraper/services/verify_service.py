"""Email verification pipeline (ARCHITECTURE.md §5.1–§5.4, §2.4).

Cheapest → most expensive, stopping as soon as the outcome is certain:

1. syntax (email-validator, ``check_deliverability=False``) → ``undeliverable/invalid_syntax``
2. suppression file (addresses + domains) → ``suppressed``, nothing else checked
3. disposable domain → ``risky/disposable`` (DNS still runs; no MX → ``undeliverable`` wins)
4. DNS: MX, A/AAAA fallback, null MX → ``undeliverable`` (no_mx / null_mx / domain_not_found)
5. role account / free provider → flags only
6. optional SMTP probe (``SmtpVerifier``, only when requested **and** ``SMTP_VERIFY_ENABLED`` with
   ``SMTP_HELO_HOST``/``SMTP_MAIL_FROM`` set) → deliverable / undeliverable / risky / unknown.
   Requested but disabled → ``unknown/smtp_disabled`` (Q9).

Without SMTP the best possible result is ``unknown`` with ``domain_has_mx: true`` (A§2.4).
Scores come from ``config/verification.yaml``. ``cached`` is always ``false``: every cache here
lives for one request/job only (A§5.4, C7).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml

from leadscraper import constants as C
from leadscraper.observability import metrics
from leadscraper.schemas.verify import (
    VerificationLevel,
    VerifyChecks,
    VerifyResult,
    VerifyResultValue,
)
from leadscraper.settings import Settings
from leadscraper.verification import lists
from leadscraper.verification.dns import DnsChecker, DnsResult
from leadscraper.verification.syntax import check_syntax

R = VerifyResultValue
L = VerificationLevel


class SmtpVerifierLike(Protocol):
    async def verify(self, email: str, mx_hosts: tuple[str, ...], *, retry_greylist: bool) -> dict: ...


@dataclass(slots=True, frozen=True)
class ScoreConfig:
    base_by_result: dict[str, float]
    base_by_reason: dict[str, float]
    adjustments: dict[str, float]


def load_score_config(path: Path | str = C.VERIFICATION_CONFIG_FILE) -> ScoreConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    as_f = lambda d: {str(k): float(v) for k, v in (d or {}).items()}  # noqa: E731
    return ScoreConfig(as_f(raw.get("base_by_result")), as_f(raw.get("base_by_reason")),
                       as_f(raw.get("adjustments")))


@lru_cache(maxsize=1)
def default_score_config() -> ScoreConfig:
    return load_score_config()


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def smtp_available(settings: Settings) -> bool:
    return bool(settings.smtp_verify_enabled and settings.smtp_helo_host and settings.smtp_mail_from)


class EmailVerifier:
    """One instance per request / job (its DNS cache is job-scoped, C7)."""

    def __init__(self, settings: Settings, *, dns: DnsChecker | None = None,
                 smtp: SmtpVerifierLike | None = None,
                 suppression: lists.SuppressionList | None = None,
                 scores: ScoreConfig | None = None, clock: Callable[[], str] = utc_now) -> None:
        self.settings = settings
        self.dns = dns or DnsChecker()
        self.smtp = smtp
        self.suppression = suppression or lists.SuppressionList()
        self.scores = scores or default_score_config()
        self.clock = clock

    def _result(self, email: str, result: R, reason: str, level: L, checks: dict[str, Any]) -> VerifyResult:
        cfg = self.scores
        score = cfg.base_by_reason.get(reason, cfg.base_by_result.get(result.value, 0.0))
        if result not in (R.UNDELIVERABLE, R.SUPPRESSED):
            if checks.get("is_role_account"):
                score += cfg.adjustments.get("role_account", 0.0)
            if checks.get("is_free_provider"):
                score += cfg.adjustments.get("free_provider", 0.0)
        return VerifyResult(email=email, result=result, reason=reason,
                            score=round(min(1.0, max(0.0, score)), 2), verification_level=level,
                            checks=VerifyChecks(**checks), cached=False, checked_at=self.clock())

    async def verify(self, email: str, *, smtp_check: bool = False,
                     retry_greylist: bool = False) -> VerifyResult:
        email = email.strip()
        syntax = check_syntax(email)
        if not syntax.valid:
            return self._result(email, R.UNDELIVERABLE, "invalid_syntax", L.SYNTAX,
                                {"syntax_valid": False})
        assert syntax.normalized and syntax.domain and syntax.local is not None
        if self.suppression.is_suppressed(syntax.normalized):
            return self._result(email, R.SUPPRESSED, "suppressed", L.SYNTAX, {"syntax_valid": True})
        disposable = lists.is_disposable(syntax.domain)
        checks: dict[str, Any] = {
            "syntax_valid": True, "is_disposable": disposable,
            "is_role_account": lists.is_role_account(syntax.local),
            "is_free_provider": lists.is_free_provider(syntax.domain),
        }
        dns: DnsResult = await self.dns.check(syntax.domain)
        checks["domain_has_mx"] = dns.has_mx
        checks["mx_hosts"] = list(dns.mx_hosts) if dns.has_mx else []
        if dns.status in ("nxdomain", "no_mx", "null_mx"):
            reason = {"nxdomain": "domain_not_found", "no_mx": "no_mx", "null_mx": "null_mx"}[dns.status]
            return self._result(email, R.UNDELIVERABLE, reason, L.DNS, checks)
        if dns.status == "error":
            return self._result(email, R.UNKNOWN, "dns_error", L.DNS, checks)
        if disposable:
            return self._result(email, R.RISKY, "disposable", L.DNS, checks)
        if not smtp_check:
            reason = "smtp_not_checked" if dns.has_mx else "a_record_fallback"
            return self._result(email, R.UNKNOWN, reason, L.DNS, checks)
        if self.smtp is None or not smtp_available(self.settings):
            return self._result(email, R.UNKNOWN, "smtp_disabled", L.DNS, checks)
        probe = await self.smtp.verify(syntax.normalized, dns.mx_hosts, retry_greylist=retry_greylist)
        return self._from_probe(email, probe, checks)

    def _from_probe(self, email: str, probe: dict, checks: dict[str, Any]) -> VerifyResult:
        """A§5.2 table: accepted → deliverable / risky(catch_all); rejected → undeliverable;
        temporary → unknown/greylisted (after the Q9 retries); blocked/timeout → unknown."""
        status = probe.get("status")
        if probe.get("code") is not None:
            checks["smtp_code"] = probe["code"]
        if status == "accepted":
            catch_all = bool(probe.get("catch_all"))
            checks["is_catch_all"] = catch_all
            out = (self._result(email, R.RISKY, "catch_all", L.SMTP, checks) if catch_all
                   else self._result(email, R.DELIVERABLE, "smtp_accepted", L.SMTP, checks))
        elif status == "rejected":
            out = self._result(email, R.UNDELIVERABLE, "smtp_rejected", L.SMTP, checks)
        elif status == "temporary":
            out = self._result(email, R.UNKNOWN, "greylisted", L.SMTP, checks)
        elif status == "blocked":
            out = self._result(email, R.UNKNOWN, "smtp_blocked", L.SMTP, checks)
        else:
            out = self._result(email, R.UNKNOWN, "smtp_timeout", L.SMTP, checks)
        metrics.SMTP_RESULTS.labels(result=status or "error").inc()
        return out

    async def verify_many(self, emails: Iterable[str], *, smtp_check: bool = False,
                          retry_greylist: bool = False) -> list[VerifyResult]:
        return [await self.verify(e, smtp_check=smtp_check, retry_greylist=retry_greylist)
                for e in emails]


def build_verifier(settings: Settings, **kwargs: Any) -> EmailVerifier:
    """Per-request/job verifier; the SMTP probe is attached only when SMTP is enabled and
    configured (``SMTP_VERIFY_ENABLED``, ``SMTP_HELO_HOST``, ``SMTP_MAIL_FROM``; A§9)."""
    from leadscraper.verification.smtp import BuiltinSmtpVerifier

    smtp = None
    if smtp_available(settings):
        smtp = BuiltinSmtpVerifier(helo_host=settings.smtp_helo_host, mail_from=settings.smtp_mail_from)
    return EmailVerifier(settings, smtp=smtp, **kwargs)
