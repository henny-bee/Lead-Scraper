"""JavaScript app-shell detection."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from leadscraper import constants as C
from leadscraper.extractors.text import page_text

APP_SHELL_MARKERS = ('id="root"', "id='root'", 'id="__next"', "id='__next'",
                     'id="app"', "id='app'", "data-reactroot", "ng-app")
_NOSCRIPT_CSR_RE = re.compile(
    r"(enable\s+javascript|javascript\s+is\s+(required|disabled)|you need to enable javascript)", re.I)
SMALL_SHELL_BYTES = 5000
LARGE_MARKUP_BYTES = 20000
SMALL_SHELL_MAX_ANCHORS = 5
SCRIPT_SHELL_MAX_ANCHORS = 3
_JS_TYPES = frozenset({"", "module", "text/javascript", "application/javascript",
                       "text/ecmascript", "application/ecmascript"})


def _has_executable_script(html: str) -> bool:
    return any((n.attributes.get("type") or "").strip().lower() in _JS_TYPES
               for n in HTMLParser(html).css("script"))


def is_js_shell(html: str) -> bool:
    """True when the page's content is evidently rendered client-side (see the module docstring)."""
    lowered = html.lower()
    anchors = lowered.count("<a ")
    if (len(html) < SMALL_SHELL_BYTES and anchors < SMALL_SHELL_MAX_ANCHORS
            and any(marker in lowered for marker in APP_SHELL_MARKERS)):
        return True
    text: str | None = None
    if "<noscript" in lowered:
        banners = [n.text(separator=" ", strip=True) for n in HTMLParser(html).css("noscript")]
        if any(_NOSCRIPT_CSR_RE.search(b) for b in banners):
            text = page_text(html)
            if len(text) < C.JS_SHELL_TEXT_MAX:
                return True
    if len(html) > LARGE_MARKUP_BYTES or ("<script" in lowered and anchors < SCRIPT_SHELL_MAX_ANCHORS
                                          and _has_executable_script(html)):
        if text is None:
            text = page_text(html)
        return len(text) < C.JS_SHELL_TEXT_MAX
    return False
