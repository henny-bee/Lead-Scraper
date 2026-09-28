# src/leadscraper/extractors/emails.py
import html
import re
from urllib.parse import unquote

import tldextract

_TLD = tldextract.TLDExtract(suffix_list_urls=())  # bundled PSL snapshot, no network call

EMAIL_RE = re.compile(r"(?<![\w.+%-])[\w.+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,24}(?![\w-])", re.I)  # PLAN Q-E9: "%" in lookbehind
MAILTO_RE = re.compile(r"""mailto:([^"'>\s]+)""", re.I)
CFEMAIL_RE = re.compile(r"""(?:data-cfemail=["']|/cdn-cgi/l/email-protection#)([0-9a-f]{8,})""", re.I)

_AT_WORDS = r"at|arroba|arobase|chiocciola"                   # en/de, es/pt, fr, it
_AT = rf"(?:\s*[\[\(\{{]\s*(?:{_AT_WORDS}|@)\s*[\]\)\}}]\s*|\s+(?:{_AT_WORDS})\s+)"
_DOT_WORDS = r"dot|punkt|punto|point"                         # en, de, es/it, fr
_DOT = rf"(?:\s*[\[\(\{{]\s*(?:{_DOT_WORDS}|\.)\s*[\]\)\}}]\s*|\s+(?:{_DOT_WORDS})\s+|\.)"
OBFUSCATED_RE = re.compile(rf"([\w.+-]+){_AT}((?:[a-z0-9-]+{_DOT})+[a-z]{{2,24}})\b", re.I)
DOT_RE = re.compile(_DOT, re.I)

JUNK_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"}
JUNK_DOMAINS = {"example.com", "example.de", "domain-example.de", "beispiel.de", "sentry.io", "wixpress.com"}


def decode_cfemail(hex_str: str) -> str:
    """Cloudflare Email Obfuscation: every byte is XOR-ed with the first byte."""
    key = int(hex_str[:2], 16)
    return "".join(chr(int(hex_str[i:i + 2], 16) ^ key) for i in range(2, len(hex_str), 2))


def _plausible(email: str) -> bool:
    local, _, domain = email.rpartition("@")
    root = _TLD(domain).top_domain_under_public_suffix  # o1.ingest.sentry.io -> sentry.io
    return bool(local) and domain.rsplit(".", 1)[-1] not in JUNK_TLDS and root not in JUNK_DOMAINS


def extract_emails(page_html: str) -> set[str]:
    text = html.unescape(page_html)                     # &#64; -> @
    found: set[str] = {decode_cfemail(h) for h in CFEMAIL_RE.findall(text)}
    for raw in MAILTO_RE.findall(text):                 # mailto:a@b-example.de?subject=...
        found.update(p.strip() for p in unquote(raw).split("?")[0].split(","))
    found.update(EMAIL_RE.findall(text))
    for local, domain in OBFUSCATED_RE.findall(text):   # info [at] firma-example [punkt] de
        found.add(f"{local}@{DOT_RE.sub('.', domain)}")
    cleaned = {e.lower().strip(".") for e in found}
    return {e for e in cleaned if _plausible(e)}
