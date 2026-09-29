"""HTML → plain text lines for the rule-based extractors (pure, no I/O)."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

_BLOCK_TAGS = "script,style,noscript,template,svg"
#: page footers (branch lists, "we deliver to …") never count as the company's own address
_FOOTER_TAGS = 'footer,[role="contentinfo"],#footer,.footer'
_WS = re.compile(r"[ \t\r\f\v   ]+")


def page_text(html: str, *, drop_footer: bool = False) -> str:
    """Visible text with one line per block/``<br>``; whitespace inside lines collapsed."""
    tree = HTMLParser(html)
    for node in tree.css(_BLOCK_TAGS):
        node.decompose()
    if drop_footer:                           # re-query: nested matches go with their parent
        while (node := tree.css_first(_FOOTER_TAGS)) is not None:
            node.decompose()
    for br in tree.css("br"):
        br.replace_with("\n")
    root = tree.body or tree.root
    raw = root.text(separator="\n") if root else ""
    lines = (_WS.sub(" ", line).strip() for line in raw.splitlines())
    return "\n".join(line for line in lines if line)


def flat(text: str) -> str:
    """Single-line version (for phrase patterns that may span lines)."""
    return " ".join(text.split())


# --- inline-joined text for the extra email pass ----------------------------------------------------
_INLINE_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "canvas", "iframe"})
_BLOCK = frozenset({
    "address", "article", "aside", "blockquote", "dd", "details", "dialog", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "menu", "nav", "ol", "p", "pre", "section", "table", "td", "th",
    "tr", "ul"})
_LONG_TOKEN = re.compile(r"\S{321,}")
_INLINE_WS = re.compile(r"[ \t\r\f\v\xa0]+")
_WORD_END = re.compile(r"\w\Z")
_WORD_START = re.compile(r"\w")


def scan_safe(text: str) -> str:
    """Drop tokens of ≥ 321 non-space characters (no email is that long), so a pathological page
    cannot make the email regexes quadratic (reference ``scan_safe_text``)."""
    return _LONG_TOKEN.sub(" ", text)


def inline_text(html: str) -> str:
    """Visible text where inline elements join without a separator (``<b>info</b>@firma.de`` →
    ``info@firma.de``) — except that two word characters meeting across a node boundary get one
    space (``<span>Mail</span><span>info@…</span>`` → ``Mail info@…``) — and block elements /
    ``<br>`` break lines (a selectolax port of the reference ``soup_to_text``)."""
    tree = HTMLParser(html)
    root = tree.body or tree.root
    if root is None:
        return ""
    parts: list[str] = []
    stack: list[tuple[object, bool]] = [(root, False)]
    while stack:
        node, closing = stack.pop()
        if closing:
            parts.append("\n")
            continue
        tag = node.tag                                         # type: ignore[attr-defined]
        if tag == "-text":
            piece = node.text(deep=False) or ""                # type: ignore[attr-defined]
            # review fix 1: two word characters meeting across a node boundary get a space ("Mail" +
            # "info@…"), splits at "@", "." or "-" still join ("info" + "@firma.de")
            if piece and parts and _WORD_END.search(parts[-1]) and _WORD_START.match(piece):
                parts.append(" ")
            parts.append(piece)
            continue
        if tag in _INLINE_SKIP or tag == "_comment":
            continue
        if tag == "br":
            parts.append("\n")
            continue
        if tag in _BLOCK:
            parts.append("\n")
            stack.append((node, True))
        stack.extend((child, False) for child in reversed(list(node.iter(include_text=True))))  # type: ignore[attr-defined]
    text = _INLINE_WS.sub(" ", "".join(parts))
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return scan_safe(text)

