"""HTML → plain text lines for the rule-based extractors (pure, no I/O)."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

_BLOCK_TAGS = "script,style,noscript,template,svg"
_WS = re.compile(r"[ \t\r\f\v   ]+")


def page_text(html: str) -> str:
    """Visible text with one line per block/``<br>``; whitespace inside lines collapsed."""
    tree = HTMLParser(html)
    for node in tree.css(_BLOCK_TAGS):
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
