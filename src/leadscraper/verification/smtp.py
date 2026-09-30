# src/leadscraper/verification/smtp.py
import asyncio
import re
import secrets


async def _reply(reader: asyncio.StreamReader) -> tuple[int, str]:
    lines: list[str] = []
    while True:
        raw = await reader.readline()
        if not raw:
            raise ConnectionError("server closed connection")
        line = raw.decode(errors="replace").rstrip("\r\n")
        lines.append(line)
        if len(line) < 4 or line[3] != "-":           # "250-..." continues, "250 ..." is the last line
            return int(line[:3]), "\n".join(lines)


def classify(code: int, message: str) -> str:
    if code in (250, 251):
        return "accepted"
    if 400 <= code < 500:
        return "temporary"                            # greylisting / rate limit -> retry later
    if re.search(r"\b5\.7\.\d+\b|block|spam|reputation|blacklist|policy", message, re.I):
        return "blocked"                              # heuristic: the verifier's IP was rejected, not the mailbox
    return "rejected"                                 # e.g. 550 5.1.1 user unknown


async def smtp_probe(mx_host: str, email: str, *, helo_host: str, mail_from: str,
                     port: int = 25, timeout: float = 15.0) -> dict:
    domain = email.rsplit("@", 1)[1]
    reader, writer = await asyncio.wait_for(asyncio.open_connection(mx_host, port), timeout)

    async def cmd(line: str) -> tuple[int, str]:
        writer.write(f"{line}\r\n".encode())
        await writer.drain()
        return await asyncio.wait_for(_reply(reader), timeout)

    try:
        code, msg = await asyncio.wait_for(_reply(reader), timeout)       # banner 220
        if code != 220:
            return {"status": classify(code, msg), "code": code, "stage": "banner"}
        code, msg = await cmd(f"EHLO {helo_host}")
        if code != 250:
            code, msg = await cmd(f"HELO {helo_host}")
        code, msg = await cmd(f"MAIL FROM:<{mail_from}>")
        if code != 250:
            return {"status": classify(code, msg), "code": code, "stage": "mail_from"}
        code, msg = await cmd(f"RCPT TO:<{email}>")
        result = {"status": classify(code, msg), "code": code, "message": msg, "catch_all": None}
        if result["status"] == "accepted":                                # catch-all detection
            probe_code, _ = await cmd(f"RCPT TO:<{secrets.token_hex(10)}@{domain}>")
            result["catch_all"] = probe_code in (250, 251)
        return result
    finally:
        try:
            await cmd("QUIT")
        except Exception:
            pass
        writer.close()


# --- below: SmtpVerifier interface, not part of the block --------------------------------------
# The code above is verbatim.
from collections.abc import Awaitable, Callable  # noqa: E402
from typing import Protocol  # noqa: E402

from leadscraper import constants as C  # noqa: E402


class SmtpVerifier(Protocol):
    async def verify(self, email: str, mx_hosts: tuple[str, ...], *, retry_greylist: bool) -> dict:
        """Probe ``email`` at the given MX hosts (lowest preference first); returns the
        ``smtp_probe`` dict (``status`` accepted | rejected | temporary | blocked | timeout)."""
        ...


class BuiltinSmtpVerifier:
    """Own implementation around:func:`smtp_probe` (never sends ``DATA``)."""

    def __init__(self, *, helo_host: str, mail_from: str, port: int = C.SMTP_PORT,
                 timeout: float = C.SMTP_TIMEOUT_S,
                 backoff_minutes: tuple[float, ...] = C.SMTP_GREYLIST_BACKOFF_MINUTES,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.helo_host, self.mail_from = helo_host, mail_from
        self.port, self.timeout = port, timeout
        self.backoff_minutes = tuple(backoff_minutes)
        self.sleep = sleep
        self._per_mx: dict[str, asyncio.Semaphore] = {}
        self.catch_all: dict[str, bool] = {}

    async def _probe_once(self, mx_host: str, email: str) -> dict:
        sem = self._per_mx.setdefault(mx_host, asyncio.Semaphore(C.SMTP_MAX_CONNECTIONS_PER_MX))
        async with sem:
            try:
                return await smtp_probe(mx_host, email, helo_host=self.helo_host,
                                        mail_from=self.mail_from, port=self.port,
                                        timeout=self.timeout)
            except (OSError, asyncio.TimeoutError, ConnectionError, ValueError) as exc:
                return {"status": "timeout", "code": None, "error": type(exc).__name__}

    async def verify(self, email: str, mx_hosts: tuple[str, ...], *, retry_greylist: bool) -> dict:
        if not mx_hosts:
            return {"status": "timeout", "code": None, "error": "no_mx"}
        mx = mx_hosts[0]
        result = await self._probe_once(mx, email)
        delays = self.backoff_minutes if retry_greylist else ()
        for minutes in delays:
            if result.get("status") != "temporary":
                break
            await self.sleep(minutes * 60)
            result = await self._probe_once(mx, email)
        domain = email.rsplit("@", 1)[1].lower()
        if result.get("catch_all") is not None:
            self.catch_all[domain] = bool(result["catch_all"])
        return result
