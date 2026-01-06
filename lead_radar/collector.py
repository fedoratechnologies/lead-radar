from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import LeadRadarConfig, load_config
from .db import (
    SignalRow,
    content_hash,
    compute_org_aggregate,
    connect,
    ensure_schema,
    get_last_signal_at,
    update_org_score,
    upsert_org,
    upsert_signal,
)
from .erpnext import ERPNextClient
from .keyword_match import match_keywords
from .org_extract import extract_org_candidate
from .raw_store import RawStore, json_safe, raw_store_from_env
from .rss import fetch_rss


def _env(name: str) -> str | None:
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _erpnext_client_from_env() -> ERPNextClient | None:
    base_url = _env("LEAD_RADAR_ERPNEXT_BASE_URL")
    api_key = _env("LEAD_RADAR_ERPNEXT_API_KEY")
    api_secret = _env("LEAD_RADAR_ERPNEXT_API_SECRET")
    if not base_url or not api_key or not api_secret:
        return None
    return ERPNextClient(base_url=base_url, api_key=api_key, api_secret=api_secret)


def collect(config: LeadRadarConfig, config_dir: Path) -> None:
    dsn = _env("LEAD_RADAR_DB_DSN")
    if not dsn:
        raise RuntimeError("LEAD_RADAR_DB_DSN is required")

    conn = connect(dsn)
    ensure_schema(conn)

    now = datetime.now(tz=timezone.utc)
    user_agent = _env("LEAD_RADAR_USER_AGENT") or "lead-radar/0.1 (+https://github.com/fedoratechnologies/lead-radar)"

    enabled_packs = [p for p in config.keyword_packs if p.enabled]
    erpnext = _erpnext_client_from_env()
    raw_store = _raw_store_init()

    touched_orgs: set[str] = set()

    for source in [s for s in config.sources if s.enabled]:
        if source.type != "rss":
            continue

        entries = fetch_rss(source.url, user_agent=user_agent)
        for e in entries:
            text = f"{e.title}\n\n{e.summary}".strip()
            hits_by_pack: dict[str, list[dict]] = {}
            score = 0.0

            for pack in enabled_packs:
                hits = match_keywords(text, pack.keywords)
                if not hits:
                    continue
                hits_by_pack[pack.id] = [asdict(h) for h in hits]
                score += sum(h.weight for h in hits)

            if score <= 0:
                continue

            org = extract_org_candidate(text)
            org_name = org.name if org else None
            org_conf = float(org.confidence) if org else None

            raw_payload: dict | None = None
            if raw_store:
                raw_payload = _store_raw_signal(
                    raw_store=raw_store,
                    fetched_at=now,
                    source_id=source.id,
                    url=e.link,
                    title=e.title,
                    summary=e.summary,
                    published_at=e.published_at,
                    org_name=org_name,
                    org_confidence=org_conf,
                    keyword_hits=hits_by_pack,
                    signal_score=score,
                    entry_raw=e.raw,
                )

            inserted = upsert_signal(
                conn,
                SignalRow(
                    source_id=source.id,
                    url=e.link,
                    title=e.title,
                    summary=e.summary,
                    published_at=e.published_at,
                    org_name=org_name,
                    org_confidence=org_conf,
                    keyword_hits=hits_by_pack,
                    signal_score=score,
                    raw=raw_payload or e.raw,
                ),
            )
            if inserted and org_name:
                upsert_org(conn, org_name)
                touched_orgs.add(org_name)

    for org_name in sorted(touched_orgs):
        aggregate = compute_org_aggregate(
            conn,
            org_name=org_name,
            now=now,
            window_days=config.scoring.window_days,
            half_life_days=config.scoring.half_life_days,
            min_signal_confidence=config.scoring.min_signal_confidence,
        )
        last_signal_at = get_last_signal_at(conn, org_name=org_name)
        promoted_at = now if aggregate >= config.scoring.promote_threshold else None
        update_org_score(
            conn,
            org_name=org_name,
            aggregate_score=aggregate,
            last_signal_at=last_signal_at,
            promoted_at=promoted_at,
        )

        if (
            erpnext
            and promoted_at
            and aggregate >= config.scoring.promote_threshold
        ):
            # MVP: create Lead if none exists for this name.
            existing = erpnext.find_lead_by_name(org_name)
            if not existing:
                erpnext.create_lead(
                    lead_name=org_name,
                    notes=f"Auto-created by Lead Radar. Score={aggregate:.2f}",
                )


def main() -> None:
    config_dir = Path(os.getenv("LEAD_RADAR_CONFIG_DIR", "/config")).resolve()
    config = load_config(config_dir)
    collect(config=config, config_dir=config_dir)


def _raw_store_init() -> RawStore | None:
    raw_store = raw_store_from_env()
    if not raw_store:
        return None
    try:
        raw_store.ensure_bucket()
    except Exception as exc:
        print(f"WARNING: Failed to init raw store bucket '{raw_store.bucket}': {exc}")
        return None
    return raw_store


def _raw_object_key(raw_store: RawStore, fetched_at: datetime, source_id: str, content_sha: str) -> str:
    date_path = fetched_at.strftime("%Y/%m/%d")
    prefix = raw_store.prefix.strip().strip("/")
    if prefix:
        return f"{prefix}/signals/{date_path}/{source_id}/{content_sha}.json"
    return f"signals/{date_path}/{source_id}/{content_sha}.json"


def _store_raw_signal(
    raw_store: RawStore,
    fetched_at: datetime,
    source_id: str,
    url: str,
    title: str,
    summary: str,
    published_at: datetime | None,
    org_name: str | None,
    org_confidence: float | None,
    keyword_hits: dict[str, list[dict]],
    signal_score: float,
    entry_raw: dict,
) -> dict[str, Any] | None:
    try:
        sha = content_hash(source_id=source_id, url=url, title=title, summary=summary)
        key = _raw_object_key(raw_store=raw_store, fetched_at=fetched_at, source_id=source_id, content_sha=sha)
        artifact = {
            "fetched_at": fetched_at.isoformat(),
            "content_hash": sha,
            "source_id": source_id,
            "url": url,
            "title": title,
            "summary": summary,
            "published_at": published_at.isoformat() if published_at else None,
            "org_name": org_name,
            "org_confidence": org_confidence,
            "keyword_hits": keyword_hits,
            "signal_score": signal_score,
            "entry_raw": entry_raw,
        }
        loc = raw_store.put_json(key=key, payload=json_safe(artifact))
        return {
            "s3": {
                "endpoint": raw_store.endpoint,
                "bucket": loc.bucket,
                "key": loc.key,
                "etag": loc.etag,
                "version_id": loc.version_id,
            }
        }
    except Exception as exc:
        print(f"WARNING: Failed to store raw signal to MinIO: {exc}")
        return None
