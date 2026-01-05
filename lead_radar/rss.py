from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import feedparser
from dateutil import parser as date_parser


@dataclass(frozen=True)
class RssEntry:
    title: str
    link: str
    summary: str
    published_at: datetime | None
    raw: dict[str, Any]


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_rss(url: str, user_agent: str) -> list[RssEntry]:
    parsed = feedparser.parse(url, agent=user_agent)
    entries: list[RssEntry] = []

    for e in parsed.entries:
        title = str(getattr(e, "title", "") or "")
        link = str(getattr(e, "link", "") or "")
        summary = str(getattr(e, "summary", "") or "")

        published = _parse_datetime(
            str(getattr(e, "published", "") or getattr(e, "updated", "") or "")
        )

        if not title or not link:
            continue
        entries.append(
            RssEntry(
                title=title,
                link=link,
                summary=summary,
                published_at=published,
                raw={k: e.get(k) for k in e.keys()},
            )
        )
    return entries

