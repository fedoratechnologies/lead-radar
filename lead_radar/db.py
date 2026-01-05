from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .org_extract import normalize_org_name
from .scoring import decay_multiplier


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
  id uuid PRIMARY KEY,
  source_id text NOT NULL,
  url text NOT NULL,
  title text NOT NULL,
  summary text,
  published_at timestamptz,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  content_hash text NOT NULL,
  org_name text,
  org_confidence real,
  keyword_hits jsonb NOT NULL DEFAULT '{}'::jsonb,
  signal_score real NOT NULL,
  raw jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS signals_source_url_idx ON signals(source_id, url);
CREATE INDEX IF NOT EXISTS signals_org_name_idx ON signals(org_name);
CREATE INDEX IF NOT EXISTS signals_published_at_idx ON signals(published_at);

CREATE TABLE IF NOT EXISTS organizations (
  id uuid PRIMARY KEY,
  name text NOT NULL UNIQUE,
  normalized_name text NOT NULL,
  aggregate_score real NOT NULL DEFAULT 0,
  last_signal_at timestamptz,
  promoted_at timestamptz,
  erpnext_lead_name text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class SignalRow:
    source_id: str
    url: str
    title: str
    summary: str
    published_at: datetime | None
    org_name: str | None
    org_confidence: float | None
    keyword_hits: dict[str, Any]
    signal_score: float
    raw: dict[str, Any]


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def content_hash(source_id: str, url: str, title: str, summary: str) -> str:
    h = hashlib.sha256()
    h.update(source_id.encode("utf-8"))
    h.update(b"\n")
    h.update(url.encode("utf-8"))
    h.update(b"\n")
    h.update(title.encode("utf-8"))
    h.update(b"\n")
    h.update(summary.encode("utf-8"))
    return h.hexdigest()


def upsert_signal(conn: psycopg.Connection, signal: SignalRow) -> bool:
    """
    Insert signal if it doesn't exist. Returns True if inserted.
    """
    signal_id = uuid.uuid4()
    chash = content_hash(signal.source_id, signal.url, signal.title, signal.summary)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signals (
              id, source_id, url, title, summary, published_at, content_hash,
              org_name, org_confidence, keyword_hits, signal_score, raw
            )
            VALUES (
              %(id)s, %(source_id)s, %(url)s, %(title)s, %(summary)s, %(published_at)s, %(content_hash)s,
              %(org_name)s, %(org_confidence)s, %(keyword_hits)s::jsonb, %(signal_score)s, %(raw)s::jsonb
            )
            ON CONFLICT (source_id, url) DO NOTHING
            """,
            {
                "id": str(signal_id),
                "source_id": signal.source_id,
                "url": signal.url,
                "title": signal.title,
                "summary": signal.summary,
                "published_at": signal.published_at,
                "content_hash": chash,
                "org_name": signal.org_name,
                "org_confidence": signal.org_confidence,
                "keyword_hits": json.dumps(signal.keyword_hits, ensure_ascii=False),
                "signal_score": float(signal.signal_score),
                "raw": json.dumps(signal.raw, ensure_ascii=False),
            },
        )
        inserted = cur.rowcount == 1
    conn.commit()
    return inserted


def upsert_org(conn: psycopg.Connection, name: str) -> uuid.UUID:
    normalized = normalize_org_name(name)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM organizations WHERE name = %(name)s", {"name": name})
        row = cur.fetchone()
        if row:
            return uuid.UUID(str(row["id"]))

        org_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO organizations (id, name, normalized_name)
            VALUES (%(id)s, %(name)s, %(normalized)s)
            """,
            {"id": str(org_id), "name": name, "normalized": normalized},
        )
    conn.commit()
    return org_id


def compute_org_aggregate(
    conn: psycopg.Connection,
    org_name: str,
    now: datetime,
    window_days: int,
    half_life_days: int,
    min_signal_confidence: float,
) -> float:
    since = now - timedelta(days=window_days)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT published_at, fetched_at, signal_score, org_confidence
            FROM signals
            WHERE org_name = %(org_name)s
              AND COALESCE(published_at, fetched_at) >= %(since)s
            """,
            {"org_name": org_name, "since": since},
        )
        rows = cur.fetchall() or []

    total = 0.0
    for r in rows:
        ts = r["published_at"] or r["fetched_at"]
        conf = float(r["org_confidence"] or 0.0)
        if conf < min_signal_confidence:
            continue
        mult = decay_multiplier(half_life_days=half_life_days, now=now, ts=ts)
        total += float(r["signal_score"]) * mult
    return total


def update_org_score(
    conn: psycopg.Connection,
    org_name: str,
    aggregate_score: float,
    last_signal_at: datetime | None,
    promoted_at: datetime | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE organizations
            SET aggregate_score = %(aggregate_score)s,
                last_signal_at = %(last_signal_at)s,
                promoted_at = COALESCE(promoted_at, %(promoted_at)s),
                updated_at = now()
            WHERE name = %(org_name)s
            """,
            {
                "org_name": org_name,
                "aggregate_score": float(aggregate_score),
                "last_signal_at": last_signal_at,
                "promoted_at": promoted_at,
            },
        )
    conn.commit()


def get_last_signal_at(conn: psycopg.Connection, org_name: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(COALESCE(published_at, fetched_at)) AS last_ts
            FROM signals
            WHERE org_name = %(org_name)s
            """,
            {"org_name": org_name},
        )
        row = cur.fetchone()
    if not row or not row["last_ts"]:
        return None
    ts = row["last_ts"]
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts

