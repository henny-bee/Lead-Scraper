"""Marketing-objection flag."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml

from leadscraper import constants as C

OBJECTION_FILE = C.CONFIG_DIR / "i18n" / "objection_patterns.yaml"


@lru_cache(maxsize=1)
def objection_patterns(path: str = str(OBJECTION_FILE)) -> dict[str, tuple[re.Pattern, ...]]:
    p = Path(path)
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.is_file() else {}
    return {str(lang): tuple(re.compile(str(x), re.I) for x in pats or [])
            for lang, pats in raw.items()}


def find_objection(texts: Iterable[str]) -> str | None:
    """The matched phrase, or None."""
    patterns = [p for pats in objection_patterns().values() for p in pats]
    for text in texts:
        flat = " ".join(text.split())
        for pattern in patterns:
            if m := pattern.search(flat):
                return m.group(0)
    return None


def has_marketing_objection(texts: Iterable[str]) -> bool:
    return find_objection(texts) is not None
