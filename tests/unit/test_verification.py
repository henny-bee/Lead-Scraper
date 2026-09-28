import socket
from pathlib import Path

import dns.exception
import dns.name
import dns.resolver
import pytest

from leadscraper.schemas.verify import VerifyResult
from leadscraper.services.verify_service import EmailVerifier, load_score_config
from leadscraper.settings import load_settings
from leadscraper.verification import lists
from leadscraper.verification.dns import DnsChecker
from leadscraper.verification.syntax import check_syntax

pytestmark = pytest.mark.anyio
SETTINGS = load_settings({})


class MX:
    def __init__(self, pref: int, host: str) -> None:
        self.preference, self.exchange = pref, dns.name.from_text(host)


class FakeResolver:
    """dnspython stand-in: table[(domain, rdtype)] = list | exception class."""

    def __init__(self, table: dict) -> None:
        self.table, self.calls = table, []

    async def resolve(self, qname: str, rdtype: str, **_kw):
        self.calls.append((qname, rdtype))
        value = self.table.get((qname, rdtype), dns.resolver.NoAnswer)
        if isinstance(value, type) and issubclass(value, Exception):
            raise value()
        return value


ZONES = {
    ("example.de", "MX"): [MX(20, "mx02.example.de."), MX(10, "mx01.example.de.")],
    ("mailinator.com", "MX"): [MX(10, "mail.mailinator.com.")],
    ("gmail.com", "MX"): [MX(5, "gmail-smtp-in.l.google.com.")],
    ("nullmx-example.de", "MX"): [MX(0, ".")],
    ("nomail-example.de", "MX"): dns.resolver.NoAnswer,
    ("aonly-example.de", "MX"): dns.resolver.NoAnswer,
    ("aonly-example.de", "A"): ["93.184.216.34"],
    ("gone-example.de", "MX"): dns.resolver.NXDOMAIN,
    ("slow-example.de", "MX"): dns.exception.Timeout,
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only loopback connections (the event loop's self-pipe on Windows) are allowed."""
    real_connect = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        raise AssertionError(f"network access in verification tests: {address}")

    def no_dns(*_a, **_k):
        raise AssertionError("DNS lookup in verification tests")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)


@pytest.fixture
def suppression(tmp_path: Path) -> lists.SuppressionList:
    f = tmp_path / "suppression.txt"
    f.write_text("# operator list\nblocked@example.de\n@optout-example.de\nwhole-domain-example.de\n", encoding="utf-8")
    return lists.SuppressionList(path=f)


def verifier(suppression: lists.SuppressionList, resolver: FakeResolver | None = None,
             settings=SETTINGS) -> EmailVerifier:
    return EmailVerifier(settings, dns=DnsChecker(resolver or FakeResolver(ZONES)),
                         suppression=suppression, clock=lambda: "2026-09-24T10:00:00Z")


def test_syntax_normalisation() -> None:
    ok = check_syntax(" Vertrieb@Übungs-Example.DE ")
    assert ok.valid and ok.domain == "xn--bungs-example-vob.de"
    assert not check_syntax("not-an-email").valid and not check_syntax("a@b").valid


async def test_bad_syntax_undeliverable(suppression) -> None:
    r = await verifier(suppression).verify("info@@example.de")
    assert (r.result, r.reason, r.verification_level) == ("undeliverable", "invalid_syntax", "syntax")
    assert r.checks.syntax_valid is False and r.score == 0.0


async def test_suppressed_stops_everything(suppression) -> None:
    resolver = FakeResolver(ZONES)
    v = verifier(suppression, resolver)
    for email in ("blocked@example.de", "anyone@optout-example.de", "x@sub.whole-domain-example.de"):
        r = await v.verify(email, smtp_check=True)
        assert r.result == "suppressed" and r.reason == "suppressed"
        assert r.checks.model_dump(exclude_none=True) == {"syntax_valid": True}
    assert resolver.calls == []                                     # nothing else checked


async def test_suppression_file_reloaded_on_change(tmp_path: Path) -> None:
    f = tmp_path / "s.txt"
    f.write_text("", encoding="utf-8")
    s = lists.SuppressionList(path=f)
    assert not s.is_suppressed("a@b-example.de")
    f.write_text("a@b-example.de\n", encoding="utf-8")
    import os
    os.utime(f, (1, 1))
    assert s.is_suppressed("A@B-EXAMPLE.de")
    assert not lists.SuppressionList(path=tmp_path / "missing.txt").is_suppressed("a@b-example.de")


async def test_disposable_is_risky(suppression) -> None:
    r = await verifier(suppression).verify("abc@mailinator.com")
    assert (r.result, r.reason) == ("risky", "disposable") and r.checks.is_disposable


async def test_no_mx_null_mx_nxdomain_undeliverable(suppression) -> None:
    v = verifier(suppression)
    assert (await v.verify("info@nomail-example.de")).reason == "no_mx"
    null = await v.verify("info@nullmx-example.de")
    assert (null.result, null.reason) == ("undeliverable", "null_mx")
    assert null.checks.domain_has_mx is False
    gone = await v.verify("info@gone-example.de")
    assert (gone.result, gone.reason, gone.verification_level) == ("undeliverable", "domain_not_found", "dns")


async def test_mx_without_smtp_is_unknown_dns_level(suppression) -> None:
    r = await verifier(suppression).verify("info@example.de")
    assert (r.result, r.reason, r.verification_level) == ("unknown", "smtp_not_checked", "dns")
    assert r.checks.domain_has_mx is True
    assert r.checks.mx_hosts == ["mx01.example.de", "mx02.example.de"]  # by preference
    assert r.checks.is_role_account is True and r.checks.is_free_provider is False
    assert r.cached is False and r.checked_at == "2026-09-24T10:00:00Z"
    assert r.score == pytest.approx(0.58)                              # 0.6 base - 0.02 role


async def test_smtp_requested_but_disabled(suppression) -> None:
    r = await verifier(suppression).verify("info@example.de", smtp_check=True)
    assert (r.result, r.reason, r.verification_level) == ("unknown", "smtp_disabled", "dns")


async def test_a_record_fallback_and_dns_error(suppression) -> None:
    v = verifier(suppression)
    a = await v.verify("info@aonly-example.de")
    assert (a.result, a.reason) == ("unknown", "a_record_fallback") and a.checks.domain_has_mx is False
    err = await v.verify("info@slow-example.de")
    assert (err.result, err.reason) == ("unknown", "dns_error")


async def test_free_provider_flag_only(suppression) -> None:
    r = await verifier(suppression).verify("anna.muster@gmail.com")
    assert r.result == "unknown" and r.checks.is_free_provider is True
    assert r.checks.is_role_account is False


async def test_dns_cache_is_per_checker_only(suppression) -> None:
    resolver = FakeResolver(ZONES)
    v = verifier(suppression, resolver)
    await v.verify("info@example.de")
    await v.verify("sales@EXAMPLE.de")
    assert resolver.calls.count(("example.de", "MX")) == 1          # cached within the job
    other = verifier(suppression, resolver)                          # new request/job
    r = await other.verify("info@example.de")
    assert resolver.calls.count(("example.de", "MX")) == 2          # no cross-job cache
    assert r.cached is False


async def test_result_shape_matches_a24(suppression) -> None:
    r = await verifier(suppression).verify("info@example.de")
    body = r.model_dump(mode="json", exclude_none=True)
    assert set(body) == {"email", "result", "reason", "score", "verification_level", "checks",
                         "cached", "checked_at"}
    assert set(body["checks"]) <= set(VerifyResult.model_fields["checks"].annotation.model_fields)


def test_score_weights_come_from_config(tmp_path: Path) -> None:
    f = tmp_path / "v.yaml"
    f.write_text("base_by_result: {unknown: 0.1}\nbase_by_reason: {}\nadjustments: {}\n", encoding="utf-8")
    cfg = load_score_config(f)
    assert cfg.base_by_result == {"unknown": 0.1}
    default = load_score_config()
    assert default.base_by_reason["smtp_accepted"] == 0.95 and default.adjustments["role_account"] == -0.02


def test_vendored_lists_loaded() -> None:
    assert len(lists.disposable_domains()) > 5000 and "mailinator.com" in lists.disposable_domains()
    assert len(lists.free_mail_domains()) > 4000 and "gmail.com" in lists.free_mail_domains()
    assert lists.is_disposable("sub.mailinator.com") and not lists.is_disposable("example.de")
    assert lists.is_role_account("info") and lists.is_role_account("vertrieb.nord")
    assert not lists.is_role_account("max.mustermann")
    header = (lists.DISPOSABLE_FILE.read_text(encoding="utf-8").splitlines()[:4])
    assert any("CC0" in line for line in header)
    assert any("MIT" in line for line in lists.FREE_MAIL_FILE.read_text(encoding="utf-8").splitlines()[:6])


def test_build_verifier_attaches_smtp_only_when_configured() -> None:
    from leadscraper.services.verify_service import build_verifier

    assert build_verifier(SETTINGS).smtp is None
    half = load_settings({"SMTP_VERIFY_ENABLED": "true"})             # HELO/MAIL FROM missing
    assert build_verifier(half).smtp is None
    full = load_settings({"SMTP_VERIFY_ENABLED": "true", "SMTP_HELO_HOST": "v.example.org",
                          "SMTP_MAIL_FROM": "b@v.example.org"})
    probe = build_verifier(full).smtp
    assert probe is not None and probe.port == 25 and probe.helo_host == "v.example.org"
