from __future__ import annotations

import os
import re
import time
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
    list_signals_for_org,
    list_org_names,
    prune_old_signals,
    update_org_score,
    update_signal_raw,
    upsert_org,
    upsert_signal,
)
from .erpnext import ERPNextClient
from .intent import match_intent
from .keyword_match import match_keywords
from .hubdirectory import hubdirectory_client_from_env
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
    site_host = _env("LEAD_RADAR_ERPNEXT_SITE_HOST") or _env("LEAD_RADAR_ERPNEXT_SITE")
    api_key = _env("LEAD_RADAR_ERPNEXT_API_KEY")
    api_secret = _env("LEAD_RADAR_ERPNEXT_API_SECRET")
    if not base_url or not api_key or not api_secret:
        return None
    return ERPNextClient(base_url=base_url, api_key=api_key, api_secret=api_secret, site_host=site_host)


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
    pack_by_id = {p.id: p for p in config.keyword_packs}
    erpnext = _erpnext_client_from_env()
    hubdirectory = hubdirectory_client_from_env()
    erpnext_company = _env("LEAD_RADAR_ERPNEXT_COMPANY") or "Fedora Technologies"
    raw_store = _raw_store_init()
    source_by_id = {s.id: s for s in config.sources}

    hub_min_score = float(_env("LEAD_RADAR_HUB_MIN_SCORE") or (config.scoring.promote_threshold or 0))
    hub_max_orgs = int(_env("LEAD_RADAR_HUB_MAX_ORGS") or "200")
    hub_batch_size = int(_env("LEAD_RADAR_HUB_BATCH_SIZE") or "100")
    hub_signal_limit = int(_env("LEAD_RADAR_HUB_SIGNAL_LIMIT") or "8")
    hub_org_updates: list[dict] = []

    inserted_signals = 0
    updated_signals = 0
    skipped_too_old = 0

    for source in [s for s in config.sources if s.enabled]:
        source_started = time.monotonic()
        entries = _fetch_entries_for_source(source, user_agent=user_agent)
        source_fetched = len(entries)
        source_inserted = 0
        source_updated = 0
        source_skipped_too_old = 0
        source_skipped_no_match = 0
        source_orgs: set[str] = set()

        for e in entries:
            if e.published_at and e.published_at < since:
                source_skipped_too_old += 1
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
                source_skipped_no_match += 1
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

            inserted, updated = upsert_signal(
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
                    raw=e.raw,
                ),
            )
            if raw_store and (inserted or updated):
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
                if raw_payload:
                    update_signal_raw(conn, source_id=source.id, url=e.link, raw=raw_payload)
            if inserted:
                inserted_signals += 1
                source_inserted += 1
            if updated:
                updated_signals += 1
                source_updated += 1
            if (inserted or updated) and org_name:
                source_orgs.add(org_name)
                upsert_org(conn, org_name)

        elapsed = time.monotonic() - source_started
        print(
            "Source summary: "
            f"id={source.id} fetched={source_fetched} inserted={source_inserted} updated={source_updated} "
            f"skipped_too_old={source_skipped_too_old} skipped_no_match={source_skipped_no_match} "
            f"orgs={len(source_orgs)} seconds={elapsed:.1f}"
        )

    deleted = prune_old_signals(conn, since=since)
    if deleted:
        print(f"Pruned {deleted} signals older than {since.isoformat()}")

    if inserted_signals or updated_signals or skipped_too_old:
        print(
            "Collector summary: "
            f"inserted={inserted_signals} updated={updated_signals} skipped_too_old={skipped_too_old} "
            f"window_days={config.scoring.window_days}"
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

        if hubdirectory and last_signal_at and aggregate >= hub_min_score and len(hub_org_updates) < hub_max_orgs:
            recent = get_recent_signals(conn, org_name=org_name, limit=hub_signal_limit)
            hub_org_updates.append(
                {
                    "org_name": org_name,
                    "aggregate_score": round(float(aggregate), 2),
                    "aggregate_raw": round(float(aggregate_raw), 4),
                    "last_signal_at": last_signal_at.isoformat(),
                    "promoted_at": promoted_at.isoformat() if promoted_at else None,
                    "signals": [_json_safe_signal_row(row) for row in (recent or [])],
                }
            )

        if not erpnext or not last_signal_at:
            continue

        try:
            # Keep Opportunity probability aligned to Lead Radar's aggregate score (0-100).
            probability = _clamp_probability(aggregate)

            signals = list_signals_for_org(conn, org_name=org_name, since=since)

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

                # Ensure the Lead has full evidence attached.
                lead_name = erpnext.find_lead_by_name(org_name)
                if lead_name:
                    _sync_evidence_notes(
                        erpnext=erpnext,
                        parenttype="Lead",
                        parent=lead_name,
                        org_name=org_name,
                        signals=signals,
                        source_by_id=source_by_id,
                        pack_by_id=pack_by_id,
                        tz=tz,
                    )

                # Close any open Opportunity for the Prospect.
                opp = erpnext.find_open_opportunity_for_prospect(org_name)
                if opp:
                    erpnext.set_opportunity_probability(opp, probability)
                    erpnext.set_opportunity_status(opp, "Converted")
            else:
                # Lower confidence: ensure a Prospect + an open Opportunity so it shows up in CRM search.
                erpnext.ensure_prospect(org_name, company=erpnext_company)
                opp = erpnext.find_open_opportunity_for_prospect(org_name)
                if not opp:
                    opp = erpnext.create_opportunity_for_prospect(
                        org_name,
                        company=erpnext_company,
                        transaction_date=now.date().isoformat(),
                        title=org_name,
                        probability=probability,
                        status="Open",
                    )
                else:
                    erpnext.set_opportunity_probability(opp, probability)

                _sync_evidence_notes(
                    erpnext=erpnext,
                    parenttype="Opportunity",
                    parent=opp,
                    org_name=org_name,
                    signals=signals,
                    source_by_id=source_by_id,
                    pack_by_id=pack_by_id,
                    tz=tz,
                )
        except Exception as exc:
            print(f"WARNING: ERPNext sync failed for org '{org_name}': {exc}")

    if hubdirectory and hub_org_updates:
        sent_total = 0
        batches = list(_chunked(hub_org_updates, hub_batch_size))
        for index, batch in enumerate(batches, start=1):
            try:
                result = hubdirectory.ingest_organizations(batch)
                sent_total += len(batch)
                inserted = int(result.get("inserted") or 0) if isinstance(result, dict) else 0
                updated = int(result.get("updated") or 0) if isinstance(result, dict) else 0
                skipped = int(result.get("skipped") or 0) if isinstance(result, dict) else 0
                print(
                    "HubDirectory ingest summary: "
                    f"batch={index}/{len(batches)} sent={len(batch)} inserted={inserted} updated={updated} skipped={skipped}"
                )
            except Exception as exc:
                print(f"WARNING: HubDirectory ingest failed (batch {index}/{len(batches)}): {exc}")
        print(f"HubDirectory ingest total: sent={sent_total} min_score={hub_min_score:g} max_orgs={hub_max_orgs}")


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
	            },
            # Also store a small, query-friendly subset inline in Postgres so ERPNext notes
            # can display evidence without fetching MinIO objects.
            "intent_hits": intent_hits,
            "score_details": score_details,
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
            max_pages=int(getattr(source, "max_pages", 1) or 1),
            page_param=str(getattr(source, "page_param", "page") or "page"),
            start_page=int(getattr(source, "start_page", 0) or 0),
        )
    print(f"WARNING: Unsupported source type '{stype}' (source_id={getattr(source, 'id', '?')})")
    return []


def _json_safe_signal_row(row: dict) -> dict:
    out: dict[str, Any] = {}
    for key in [
        "source_id",
        "content_hash",
        "title",
        "url",
        "summary",
        "signal_score",
        "org_confidence",
        "keyword_hits",
        "published_at",
        "fetched_at",
    ]:
        value = (row or {}).get(key)
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return json_safe(out) if isinstance(out, dict) else {}


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    n = max(1, int(size or 1))
    return [items[i : i + n] for i in range(0, len(items), n)]


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


_EVIDENCE_HASH_RE = re.compile(r"LeadRadarHash\\s*[:=]\\s*([0-9a-f]{64})", flags=re.IGNORECASE)


def _clamp_probability(value: float) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    if v < 0:
        v = 0.0
    if v > 100:
        v = 100.0
    return round(v, 1)


def _extract_existing_hashes(notes: list[dict] | None) -> set[str]:
    out: set[str] = set()
    for row in notes or []:
        text = str((row or {}).get("note") or "")
        m = _EVIDENCE_HASH_RE.search(text)
        if not m:
            continue
        out.add(m.group(1).lower())
    return out


def _format_keyword_hits(keyword_hits: dict, pack_by_id: dict[str, Any]) -> str:
    if not keyword_hits:
        return ""
    lines: list[str] = []
    for pack_id, hits in (keyword_hits or {}).items():
        pack_name = None
        pack = pack_by_id.get(str(pack_id))
        if pack:
            pack_name = getattr(pack, "name", None)
        label = f"{pack_id}" + (f" ({pack_name})" if pack_name else "")
        kws: list[str] = []
        for hit in hits or []:
            kw = str((hit or {}).get("keyword") or "").strip()
            if not kw:
                continue
            w = (hit or {}).get("weight")
            try:
                wv = float(w)
            except Exception:
                wv = None
            kws.append(f"{kw}" + (f" (w={wv:g})" if wv is not None else ""))
        if kws:
            lines.append(f"- {label}: " + ", ".join(kws))
    return "\n".join(lines).strip()


def _format_intent_hits(intent_hits: list[dict] | None) -> str:
    if not intent_hits:
        return ""
    parts: list[str] = []
    for hit in intent_hits:
        label = str((hit or {}).get("label") or (hit or {}).get("intent") or "").strip()
        if not label:
            continue
        w = (hit or {}).get("weight")
        try:
            wv = float(w)
        except Exception:
            wv = None
        parts.append(f"{label}" + (f" (w={wv:g})" if wv is not None else ""))
    if not parts:
        return ""
    return "- " + ", ".join(parts)


def _sync_evidence_notes(
    *,
    erpnext: ERPNextClient,
    parenttype: str,
    parent: str,
    org_name: str,
    signals: list[dict],
    source_by_id: dict[str, Any],
    pack_by_id: dict[str, Any],
    tz: timezone | ZoneInfo,
) -> None:
    if not signals:
        return

    doc = erpnext.get_resource(parenttype, parent, fields=["name", "notes"])
    existing_hashes = _extract_existing_hashes(doc.get("notes"))

    created = 0
    for s in signals:
        chash = str(s.get("content_hash") or "").strip().lower()
        if not chash or len(chash) != 64:
            continue
        if chash in existing_hashes:
            continue

        source_id = str(s.get("source_id") or "").strip()
        src = source_by_id.get(source_id)
        src_name = getattr(src, "name", None) if src else None
        src_type = getattr(src, "type", None) if src else None
        src_tags = getattr(src, "tags", None) if src else None
        src_weight = getattr(src, "weight", None) if src else None

        ts = s.get("published_at") or s.get("fetched_at")
        if ts is not None and getattr(ts, "tzinfo", None) is not None:
            ts_local = ts.astimezone(tz)
            ts_str = ts_local.isoformat()
        elif ts is not None:
            ts_str = str(ts)
        else:
            ts_str = ""

        title = str(s.get("title") or "").strip()
        url = str(s.get("url") or "").strip()
        summary = str(s.get("summary") or "").strip()
        if len(summary) > 600:
            summary = summary[:597].rstrip() + "..."

        try:
            score = float(s.get("signal_score") or 0.0)
        except Exception:
            score = 0.0

        conf = s.get("org_confidence")
        try:
            conf_f = float(conf) if conf is not None else None
        except Exception:
            conf_f = None

        raw = s.get("raw") or {}
        if not isinstance(raw, dict):
            raw = {}
        s3 = raw.get("s3") if isinstance(raw.get("s3"), dict) else None
        intent_hits = raw.get("intent_hits") if isinstance(raw.get("intent_hits"), list) else None
        score_details = raw.get("score_details") if isinstance(raw.get("score_details"), dict) else None

        lines: list[str] = []
        lines.append("Lead Radar Evidence")
        lines.append(f"LeadRadarHash: {chash}")
        lines.append(f"Org: {org_name}" + (f" (conf={conf_f:.2f})" if conf_f is not None else ""))
        if source_id:
            src_line = f"Source: {source_id}"
            if src_name:
                src_line += f" ({src_name})"
            if src_type:
                src_line += f" type={src_type}"
            if src_tags:
                src_line += " tags=" + ",".join([str(t) for t in (src_tags or []) if str(t).strip()])
            if src_weight is not None:
                try:
                    src_line += f" weight={float(src_weight):g}"
                except Exception:
                    pass
            lines.append(src_line)
        if ts_str:
            lines.append(f"Published: {ts_str}")
        lines.append(f"Signal score: {score:.2f}")
        if score_details:
            base = (
                float(score_details.get("keyword_score") or 0)
                + float(score_details.get("intent_score") or 0)
                + float(score_details.get("diversity_bonus") or 0)
            )
            lines.append(f"Score details: base={base:.2f} mult={float(score_details.get('total_multiplier') or 1.0):.2f}")
        if url:
            lines.append(f"URL: {url}")
        if title:
            lines.append(f"Title: {title}")
        if summary:
            lines.append("")
            lines.append("Summary:")
            lines.append(summary)

        kw_block = _format_keyword_hits(s.get("keyword_hits") or {}, pack_by_id=pack_by_id)
        if kw_block:
            lines.append("")
            lines.append("Keyword hits:")
            lines.append(kw_block)

        intent_block = _format_intent_hits(intent_hits)
        if intent_block:
            lines.append("")
            lines.append("Intent hits:")
            lines.append(intent_block)

        if s3:
            endpoint = str(s3.get("endpoint") or "").strip()
            bucket = str(s3.get("bucket") or "").strip()
            key = str(s3.get("key") or "").strip()
            if bucket and key:
                lines.append("")
                lines.append("Raw artifact:")
                lines.append(f"- s3://{bucket}/{key}" + (f" (endpoint={endpoint})" if endpoint else ""))

        note_text = "\n".join([ln for ln in lines if ln is not None]).strip()
        erpnext.create_crm_note(parenttype=parenttype, parent=parent, note=note_text)
        existing_hashes.add(chash)
        created += 1

    if created:
        print(f"ERPNext evidence sync: parent={parenttype}/{parent} org='{org_name}' created_notes={created}")
