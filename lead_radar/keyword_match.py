from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Keyword


@dataclass(frozen=True)
class KeywordHit:
    keyword: str
    weight: float


def match_keywords(text: str, keywords: list[Keyword]) -> list[KeywordHit]:
    text_l = text.lower()
    hits: list[KeywordHit] = []
    for kw in keywords:
        needle = kw.keyword.strip().lower()
        if not needle:
            continue

        if len(needle) <= 3:
            if re.search(rf"\\b{re.escape(needle)}\\b", text_l):
                hits.append(KeywordHit(keyword=kw.keyword, weight=float(kw.weight)))
            continue

        if needle in text_l:
            hits.append(KeywordHit(keyword=kw.keyword, weight=float(kw.weight)))

    # De-dupe by keyword (keep max weight)
    by_kw: dict[str, float] = {}
    for hit in hits:
        by_kw[hit.keyword] = max(by_kw.get(hit.keyword, float("-inf")), hit.weight)
    return [KeywordHit(keyword=k, weight=w) for k, w in by_kw.items()]

