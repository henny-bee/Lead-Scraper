"""SMTP probe against a local aiosmtpd server."""

import asyncio
import socket
from pathlib import Path

import dns.name
import pytest
from aiosmtpd.controller import Controller

from leadscraper import constants as C
from leadscraper.services.verify_service import EmailVerifier
from leadscraper.settings import load_settings
from leadscraper.verification import lists, smtp
from leadscraper.verification.dns import DnsChecker
from leadscraper.verification.smtp import BuiltinSmtpVerifier, classify, smtp_probe

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[2]


class Handler:
    def __init__(self) -> None:
        self.valid = {"info@firma-probe-example.de"}
        self.catch_all_domains = {"catchall-firma-example.de"}
        self.greylist_left = 0
        self.data_seen = False
        self.rcpts: list[str] = []
        self.mail_from: list[str] = []

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        self.mail_from.append(address)
        envelope.mail_from = address
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        self.rcpts.append(address)
        if self.greylist_left > 0:
            self.greylist_left -= 1
            return "451 4.7.1 Greylisted, please try again later"
        domain = address.rsplit("@", 1)[1]
        if address in self.valid or domain in self.catch_all_domains:
            envelope.rcpt_tos.append(address)
            return "250 OK"
        if domain == "blocked-firma-example.de":
            return "554 5.7.1 Service unavailable; client host blocked using Spamhaus"
        return "550 5.1.1 <%s>: Recipient address rejected: User unknown" % address

    async def handle_DATA(self, server, session, envelope):
        self.data_seen = True                    # must never happen
        return "250 OK"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    handler = Handler()
    controller = Controller(handler, hostname="127.0.0.1", port=free_port())
    controller.start()
    try:
        yield handler, controller.port
    finally:
        controller.stop()


@pytest.fixture(autouse=True)
def only_local_connections(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, int]] = []
    real = asyncio.open_connection

    async def guarded(host=None, port=None, **kw):
        seen.append((host, port))
        assert host == "127.0.0.1" and port != 25, f"outbound SMTP attempted: {host}:{port}"
        return await real(host, port, **kw)

    monkeypatch.setattr(asyncio, "open_connection", guarded)
    yield seen


class MX:
    def __init__(self, host: str) -> None:
        self.preference, self.exchange = 10, dns.name.from_text(host)


class LocalResolver:
    async def resolve(self, qname, rdtype, **_kw):
        return [MX("127.0.0.1.")]


def make_verifier(port: int, **kw) -> EmailVerifier:
    settings = load_settings({"SMTP_VERIFY_ENABLED": "true", "SMTP_HELO_HOST": "verify.example.org",
                              "SMTP_MAIL_FROM": "bounce@verify.example.org"})
    probe = BuiltinSmtpVerifier(helo_host=settings.smtp_helo_host, mail_from=settings.smtp_mail_from,
                                port=port, timeout=5, **kw)
    return EmailVerifier(settings, dns=DnsChecker(LocalResolver()), smtp=probe,
                         suppression=lists.SuppressionList(path=ROOT / "nonexistent.txt"))


async def test_valid_mailbox_deliverable(server) -> None:
    handler, port = server
    r = await make_verifier(port).verify("info@firma-probe-example.de", smtp_check=True)
    assert (r.result, r.reason, r.verification_level) == ("deliverable", "smtp_accepted", "smtp")
    assert r.checks.smtp_code == 250 and r.checks.is_catch_all is False
    assert r.score == pytest.approx(0.93)                      # example: role account
    assert handler.mail_from == ["bounce@verify.example.org"]
    assert handler.rcpts[0] == "info@firma-probe-example.de" and len(handler.rcpts) == 2   # + random probe
    assert not handler.data_seen


async def test_unknown_mailbox_undeliverable(server) -> None:
    handler, port = server
    r = await make_verifier(port).verify("nobody@firma-probe-example.de", smtp_check=True)
    assert (r.result, r.reason) == ("undeliverable", "smtp_rejected")
    assert r.checks.smtp_code == 550 and len(handler.rcpts) == 1   # no catch-all probe after reject
    assert not handler.data_seen


async def test_catch_all_domain_risky(server) -> None:
    handler, port = server
    v = make_verifier(port)
    r = await v.verify("whoever@catchall-firma-example.de", smtp_check=True)
    assert (r.result, r.reason) == ("risky", "catch_all") and r.checks.is_catch_all is True
    assert v.smtp.catch_all == {"catchall-firma-example.de": True}             # per-job catch-all cache
    assert not handler.data_seen


async def test_blocked_verifier_is_unknown(server) -> None:
    _, port = server
    r = await make_verifier(port).verify("x@blocked-firma-example.de", smtp_check=True)
    assert (r.result, r.reason) == ("unknown", "smtp_blocked") and r.checks.smtp_code == 554


async def test_greylisting_sync_single_attempt(server) -> None:
    handler, port = server
    handler.greylist_left = 1
    r = await make_verifier(port).verify("info@firma-probe-example.de", smtp_check=True, retry_greylist=False)
    assert (r.result, r.reason) == ("unknown", "greylisted") and r.checks.smtp_code == 451
    assert handler.rcpts == ["info@firma-probe-example.de"]


async def test_greylisting_async_backoff_then_success(server) -> None:
    handler, port = server
    handler.greylist_left = 2
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    r = await make_verifier(port, sleep=fake_sleep).verify("info@firma-probe-example.de", smtp_check=True,
                                                           retry_greylist=True)
    assert slept == [5 * 60, 15 * 60]                              # constants: 5, 15, (60) min
    assert r.result == "deliverable"


async def test_greylisting_async_exhausted_is_unknown(server) -> None:
    handler, port = server
    handler.greylist_left = 10
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    r = await make_verifier(port, sleep=fake_sleep).verify("info@firma-probe-example.de", smtp_check=True,
                                                           retry_greylist=True)
    assert slept == [m * 60 for m in C.SMTP_GREYLIST_BACKOFF_MINUTES]
    assert (r.result, r.reason) == ("unknown", "greylisted")


async def test_connection_refused_is_unknown(only_local_connections) -> None:
    r = await make_verifier(free_port()).verify("info@firma-probe-example.de", smtp_check=True)
    assert (r.result, r.reason) == ("unknown", "smtp_timeout")


async def test_raw_probe_never_sends_data(server) -> None:
    handler, port = server
    res = await smtp_probe("127.0.0.1", "info@firma-probe-example.de", helo_host="h.example.org",
                           mail_from="b@h.example.org", port=port, timeout=5)
    assert res["status"] == "accepted" and res["catch_all"] is False
    assert not handler.data_seen


def test_classify_table() -> None:
    assert classify(250, "OK") == "accepted" and classify(251, "") == "accepted"
    assert classify(451, "greylisted") == "temporary"
    assert classify(550, "5.1.1 user unknown") == "rejected"
    assert classify(554, "5.7.1 blocked by spamhaus") == "blocked"


def test_smtp_module_is_a52_block_plus_interface() -> None:
    arch = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    start = arch.index("``` python\n# src/leadscraper/verification/smtp.py")
    block = arch[start + len("``` python\n"):arch.index("```", start + 10)]
    src = Path(smtp.__file__).read_text(encoding="utf-8")
    assert src.startswith(block)
    assert 'cmd("DATA' not in src and "DATA\\r\\n" not in src           # DATA never sent
