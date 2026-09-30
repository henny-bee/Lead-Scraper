"""Steps 2, 3, 5 — suppression, disposable, role and free-mail lists."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from leadscraper import constants as C
from leadscraper.extractors.scoring import default_config

DATA_DIR = Path(__file__).resolve().parent / "data"
DISPOSABLE_FILE = DATA_DIR / "disposable_domains.txt"
FREE_MAIL_FILE = DATA_DIR / "free_email_domains.txt"


def read_list(path: Path) -> frozenset[str]:
    if not path.is_file():
        return frozenset()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.split("#", 1)[0].strip().lower()
        if item:
            out.add(item)
    return frozenset(out)


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    return read_list(DISPOSABLE_FILE)


@lru_cache(maxsize=1)
def free_mail_domains() -> frozenset[str]:
    return read_list(FREE_MAIL_FILE)


@lru_cache(maxsize=1)
def role_local_parts() -> frozenset[str]:
    cfg = default_config()
    return frozenset({w for words in cfg.generic.values() for w in words} | cfg.special)


def _parents(domain: str) -> list[str]:
    parts = domain.lower().split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


def is_disposable(domain: str) -> bool:
    return any(d in disposable_domains() for d in _parents(domain))


def is_free_provider(domain: str) -> bool:
    return domain.lower() in free_mail_domains()


def is_role_account(local: str) -> bool:
    low = local.lower()
    head = low.replace("-", ".").replace("_", ".").replace("+", ".").split(".", 1)[0]
    return low in role_local_parts() or head in role_local_parts()


@dataclass
class SuppressionList:
    """Addresses and domains from the suppression file (``user@x-example.de``, ``x-example.de`` or
    ``@x-example.de``)."""

    path: Path = C.SUPPRESSION_FILE
    addresses: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    _mtime: float | None = field(default=None, repr=False)

    def refresh(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            self.addresses, self.domains, self._mtime = frozenset(), frozenset(), None
            return
        if mtime == self._mtime:
            return
        entries = read_list(self.path)
        self.addresses = frozenset(e for e in entries if "@" in e and not e.startswith("@"))
        self.domains = frozenset(e.lstrip("@") for e in entries if e.startswith("@") or "@" not in e)
        self._mtime = mtime

    def is_suppressed(self, email: str) -> bool:
        self.refresh()
        email = email.lower()
        domain = email.rpartition("@")[2]
        return email in self.addresses or any(d in self.domains for d in _parents(domain))
