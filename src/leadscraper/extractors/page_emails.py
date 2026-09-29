"""Extra email pass."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import tldextract
import yaml
from selectolax.parser import HTMLParser

from leadscraper import constants as C
from leadscraper.extractors import emails as emails_mod
from leadscraper.extractors.scoring import default_config
from leadscraper.extractors.text import inline_text

PLACEHOLDERS_FILE = C.CONFIG_DIR / "i18n" / "email_placeholders.yaml"
_TLD = tldextract.TLDExtract(suffix_list_urls=())          # bundled PSL snapshot, no network call

_LOOKALIKES = str.maketrans({"＠": "@", "﹫": "@", "．": ".", "。": ".", "․": "."})
#: Guarded spaced form (reference ``_SPACED_REGION_RE``/``_spaced_region_sub``): a region such as
#: ``john@acme dot com`` / ``hr at acme dot com`` is rewritten only if the result is an email.
_SPACED_REGION = re.compile(r"[\w@]+(?:[.+-][\w@]+)*(?:\s+(?:at|dot)\s+[\w@]+(?:[.+-][\w@]+)*)+",
                            re.IGNORECASE)
_SPACED_AT = re.compile(r"(?<=\w)\s+at\s+(?=\w)", re.IGNORECASE)
_SPACED_DOT = re.compile(r"(?<=\w)\s+dot\s+(?=\w)", re.IGNORECASE)
_EMAIL_FULL = re.compile(r"[\w.+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,24}", re.IGNORECASE)
#: runs the regexes would scan quadratically (OBFUSCATED_RE has no lookbehind); no email has a ≥
#: 321-character local/domain run, so removing them from the raw HTML loses nothing.
_LONG_RUN = re.compile(r"[\w.+-]{321,}")
_HEX_RUN = re.compile(r"[0-9a-f]{24}", re.IGNORECASE)
#: every address or obfuscated address contains "@" or an "at" word
_EMAIL_HINT = re.compile(r"@|\b(?:at|arroba|arobase|chiocciola)\b", re.IGNORECASE)
_ASSET_TLDS = frozenset({"ico"})                             # asset extensions missing in JUNK_TLDS


@dataclass(slots=True, frozen=True)
class Placeholders:
    locals: frozenset[str]
    prefixes: tuple[str, ...]
    protected: frozenset[str]
    domains: frozenset[str]
    labels: frozenset[str]                                  # from "example.*" entries


@lru_cache(maxsize=1)
def placeholders(path: Path | str = PLACEHOLDERS_FILE) -> Placeholders:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    domains = [str(d).lower() for d in raw.get("domains") or []]
    generic = {w for words in default_config().generic.values() for w in words}
    protected = generic | {str(x).lower() for x in raw.get("protected_locals") or []}
    return Placeholders(
        locals=frozenset(str(x).lower() for x in raw.get("locals") or []) - protected,
        prefixes=tuple(str(x).lower() for x in raw.get("prefixes") or []),
        protected=frozenset(protected),
        domains=frozenset(d for d in domains if not d.endswith(".*")),
        labels=frozenset(d[:-2] for d in domains if d.endswith(".*")))


def is_placeholder_domain(domain: str, config: Placeholders | None = None) -> bool:
    cfg = config or placeholders()
    domain = domain.lower().strip(".")
    root = _TLD(domain).top_domain_under_public_suffix or domain
    return domain in cfg.domains or root in cfg.domains or root.split(".", 1)[0] in cfg.labels


def is_placeholder(email: str, config: Placeholders | None = None) -> bool:
    """Placeholder domain → always; protected (generic role) locals → never on their own; otherwise
    an exact placeholder local part or one of the prefixes."""
    cfg = config or placeholders()
    local, _, domain = email.lower().rpartition("@")
    if is_placeholder_domain(domain, cfg):
        return True
    if local in cfg.protected:
        return False
    return local in cfg.locals or local.startswith(cfg.prefixes)


def plausible(email: str) -> bool:
    local, _, domain = email.rpartition("@")
    if not local or len(local) > 64 or _HEX_RUN.search(local):
        return False
    if domain.rsplit(".", 1)[-1].lower() in _ASSET_TLDS:
        return False
    return not is_placeholder(email)


def normalize_lookalikes(text: str) -> str:
    return text.translate(_LOOKALIKES)


def spaced_deobfuscate(text: str) -> str:
    def rewrite(m: re.Match[str]) -> str:
        token = _SPACED_DOT.sub(".", _SPACED_AT.sub("@", m.group(0)))
        return token if _EMAIL_FULL.fullmatch(token) else m.group(0)
    return _SPACED_REGION.sub(rewrite, text)


def microdata_emails(html: str) -> set[str]:
    out: set[str] = set()
    for node in HTMLParser(html).css("[itemprop=email]"):
        value = (node.attributes.get("content") or node.text(separator=" ") or "").strip()
        if value.lower().startswith("mailto:"):
            value = value[len("mailto:"):]
        out |= emails_mod.extract_emails(f"mailto:{value}") if value else set()
    return out


#: review fix 2: the raw pass runs only on the ``<``-separated chunks that can hold an address (an
#: address, entity, ``mailto:`` or Cloudflare token never spans a ``<``).
_RAW_HINT = re.compile(r"@|&#0*64;|&#x0*40;|&commat;|%40|cfemail|email-protection"
                       r"|\b(?:at|arroba|arobase|chiocciola)\b", re.IGNORECASE)
#: lower-case substrings every ``_RAW_HINT`` match contains (a cheap prefilter before the regex)
_RAW_LITERALS = ("@", "&#", "&commat;", "%40", "cfemail", "email-protection",
                 "at", "arroba", "arobase", "chiocciola")


def _raw_chunks(html: str) -> str:
    """The ``<``-separated chunks of the raw HTML that match ``_RAW_HINT``, joined by newlines."""
    keep = []
    for chunk in html.split("<"):
        low = chunk.lower()
        if any(lit in low for lit in _RAW_LITERALS) and _RAW_HINT.search(chunk):
            keep.append(chunk)
    return "\n".join(keep)


def _hint_lines(text: str) -> str:
    """The lines of the inline text that can hold an address."""
    return "\n".join(line for line in text.split("\n") if _EMAIL_HINT.search(line))


def extract_page_emails(html: str) -> set[str]:
    """All plausible addresses of one page (see the module docstring)."""
    # look-alikes are normalised in the raw pass too, so "a․b@x.de" never yields a truncated "b@x.de"
    found = emails_mod.extract_emails(_raw_chunks(normalize_lookalikes(_LONG_RUN.sub(" ", html))))
    text = _hint_lines(normalize_lookalikes(inline_text(html)))
    if text:
        found |= emails_mod.extract_emails(text)
        found |= emails_mod.extract_emails(spaced_deobfuscate(text))
    found |= microdata_emails(html)
    return {e for e in found if plausible(e)}
