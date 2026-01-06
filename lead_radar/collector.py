from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import LeadRadarConfig, load_config
from .db import (
    SignalRow,
    content_hash,
    compute_org_aggregate,
    connect,
    ensure_schema,
    get_recent_signals,
    get_last_signal_at,
    list_org_names,
    prune_old_signals,
    update_org_score,
    upsert_org,
    upsert_signal,
)
from .erpnext import ERPNextClient
from .intent import match_intent
from .keyword_match import match_keywords
from .org_extract import extract_org_candidate
from .raw_store import RawStore, json_safe, raw_store_from_env
from .rss import fetch_rss
from .scoring import score_to_percent
from .web import fetch_html_list, fetch_sitemap


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

    try:
        tz = ZoneInfo(str(config.scoring.time_zone or "UTC"))
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz=tz)
    since = now - timedelta(days=config.scoring.window_days)
    user_agent = _env("LEAD_RADAR_USER_AGENT") or "lead-radar/0.1 (+https://github.com/fedoratechnologies/lead-radar)"

    enabled_packs = [p for p in config.keyword_packs if p.enabled]
    erpnext = _erpnext_client_from_env()
    erpnext_company = _env("LEAD_RADAR_ERPNEXT_COMPANY") or "Fedora Technologies"
    raw_store = _raw_store_init()

    inserted_signals = 0
    skipped_too_old = 0

    for source in [s for s in config.sources if s.enabled]:
        entries = _fetch_entries_for_source(source, user_agent=user_agent)
        for e in entries:
            if e.published_at and e.published_at < since:
                skipped_too_old += 1
                continue

            text = f"{e.title}\n\n{e.summary}".strip()
            hits_by_pack: dict[str, list[dict]] = {}
            keyword_score = 0.0

            for pack in enabled_packs:
                hits = match_keywords(text, pack.keywords)
                if not hits:
                    continue
                hits_by_pack[pack.id] = [asdict(h) for h in hits]
                keyword_score += sum(h.weight for h in hits)

            intent_hits = match_intent(text)
            intent_score = sum(h.weight for h in intent_hits)

            if (keyword_score + intent_score) <= 0:
                continue

            org = extract_org_candidate(text)
            org_name = org.name if org else None
            org_conf = float(org.confidence) if org else None

            score_details = _score_signal(
                source_weight=float(source.weight),
                source_tags=source.tags,
                keyword_score=keyword_score,
                intent_score=intent_score,
                packs_hit_count=len(hits_by_pack),
                org_confidence=org_conf,
            )
            signal_score = score_details["signal_score"]

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
                    intent_hits=[asdict(h) for h in intent_hits],
                    score_details=score_details,
                    signal_score=signal_score,
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
                    signal_score=signal_score,
                    raw=raw_payload or e.raw,
                ),
            )
            if inserted:
                inserted_signals += 1
            if inserted and org_name:
                upsert_org(conn, org_name)

    deleted = prune_old_signals(conn, since=since)
    if deleted:
        print(f"Pruned {deleted} signals older than {since.isoformat()}")

    if inserted_signals or skipped_too_old:
        print(
            "Collector summary: "
            f"inserted={inserted_signals} skipped_too_old={skipped_too_old} window_days={config.scoring.window_days}"
        )

    for org_name in list_org_names(conn):
        aggregate_raw = compute_org_aggregate(
            conn,
            org_name=org_name,
            now=now,
            window_days=config.scoring.window_days,
            half_life_days=config.scoring.half_life_days,
            min_signal_confidence=config.scoring.min_signal_confidence,
        )
        aggregate = score_to_percent(aggregate_raw, scale=float(config.scoring.score_scale or 16.0))
        last_signal_at = get_last_signal_at(conn, org_name=org_name)
        promoted_at = now if aggregate >= config.scoring.promote_threshold else None
        update_org_score(
            conn,
            org_name=org_name,
            aggregate_score=aggregate,
            last_signal_at=last_signal_at,
            promoted_at=promoted_at,
        )

        if not erpnext or not last_signal_at:
            continue

        if aggregate >= config.scoring.promote_threshold:
            # High confidence: create Lead (if missing) and close any open Opportunity for this Prospect.
            existing = erpnext.find_lead_by_name(org_name)
            if not existing:
                recent = get_recent_signals(conn, org_name=org_name, limit=3)
                lines = [
                    "Auto-created by Lead Radar.",
                    f"Aggregate score: {aggregate:.2f} (raw={aggregate_raw:.2f})",
                    "",
                    "Recent signals:",
                ]
                for r in recent:
                    ts = (r.get("published_at") or r.get("fetched_at"))
                    ts_str = ts.isoformat() if ts else ""
                    title = str(r.get("title") or "").strip()
                    url = str(r.get("url") or "").strip()
                    score = float(r.get("signal_score") or 0.0)
                    lines.append(f"- {ts_str} score={score:.1f} {title} ({url})")
                erpnext.create_lead(
                    lead_name=org_name,
                    notes="\n".join(lines).strip(),
                    status="Lead",
                )

            opp = erpnext.find_open_opportunity_for_prospect(org_name)
            if opp:
                erpnext.set_opportunity_status(opp, "Converted")
        else:
            # Lower confidence: ensure a Prospect + an open Opportunity so it shows up in CRM search.
            erpnext.ensure_prospect(org_name, company=erpnext_company)
            opp = erpnext.find_open_opportunity_for_prospect(org_name)
            if not opp:
                erpnext.create_opportunity_for_prospect(
                    org_name,
                    company=erpnext_company,
                    transaction_date=now.date().isoformat(),
                    title=org_name,
                    status="Open",
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
    intent_hits: list[dict],
    score_details: dict,
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
            "intent_hits": intent_hits,
            "score_details": score_details,
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


def _fetch_entries_for_source(source: Any, user_agent: str) -> list[Any]:
    stype = str(getattr(source, "type", "") or "").strip().lower()
    if stype == "rss":
        return fetch_rss(source.url, user_agent=user_agent)
    if stype == "sitemap":
        return fetch_sitemap(
            sitemap_url=source.url,
            user_agent=user_agent,
            max_items=int(getattr(source, "max_items", 20) or 20),
            include_regex=getattr(source, "include_regex", None),
            exclude_regex=getattr(source, "exclude_regex", None),
        )
    if stype in {"html_list", "html"}:
        return fetch_html_list(
            listing_url=source.url,
            user_agent=user_agent,
            max_items=int(getattr(source, "max_items", 20) or 20),
            include_regex=getattr(source, "include_regex", None),
            exclude_regex=getattr(source, "exclude_regex", None),
        )
    print(f"WARNING: Unsupported source type '{stype}' (source_id={getattr(source, 'id', '?')})")
    return []


def _score_signal(
    source_weight: float,
    source_tags: list[str],
    keyword_score: float,
    intent_score: float,
    packs_hit_count: int,
    org_confidence: float | None,
) -> dict[str, Any]:
    tag_multiplier = 1.0
    applied: list[dict[str, Any]] = []
    for tag in [t.strip().lower() for t in (source_tags or []) if str(t).strip()]:
        mult = _TAG_MULTIPLIERS.get(tag, 1.0)
        if mult != 1.0:
            applied.append({"tag": tag, "multiplier": mult})
        tag_multiplier *= mult

    diversity_bonus = float(packs_hit_count) * 2.0 if packs_hit_count > 1 else 0.0
    base = float(keyword_score) + float(intent_score) + diversity_bonus

    total_mult = float(source_weight or 1.0) * tag_multiplier
    if org_confidence is not None:
        total_mult *= float(org_confidence)

    return {
        "keyword_score": float(keyword_score),
        "intent_score": float(intent_score),
        "diversity_bonus": float(diversity_bonus),
        "source_weight": float(source_weight or 1.0),
        "tag_multiplier": float(tag_multiplier),
        "tag_multiplier_rules": applied,
        "org_confidence_multiplier": float(org_confidence) if org_confidence is not None else None,
        "total_multiplier": float(total_mult),
        "signal_score": float(base) * float(total_mult),
    }


_TAG_MULTIPLIERS: dict[str, float] = {
    # High-intent sources
    "procurement": 1.6,
    "tender": 1.6,
    "rfp": 1.6,
    "bid": 1.4,
    "official": 1.3,
    # Medium-intent sources
    "jobs": 1.25,
    "job": 1.25,
    "careers": 1.2,
    "hiring": 1.2,
    "announcements": 1.15,
    "announcement": 1.15,
    # Default tags
    "news": 1.0,
}
