from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ScoringConfig:
    time_zone: str
    window_days: int
    half_life_days: int
    min_signal_confidence: float
    promote_threshold: float


@dataclass(frozen=True)
class Source:
    id: str
    enabled: bool
    type: str
    name: str
    url: str
    tags: list[str]
    weight: float
    max_items: int
    include_regex: str | None
    exclude_regex: str | None


@dataclass(frozen=True)
class Keyword:
    keyword: str
    weight: float


@dataclass(frozen=True)
class KeywordPack:
    id: str
    enabled: bool
    name: str
    tags: list[str]
    keywords: list[Keyword]


@dataclass(frozen=True)
class LeadRadarConfig:
    scoring: ScoringConfig
    sources: list[Source]
    keyword_packs: list[KeywordPack]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}")
    return data


def load_config(config_dir: Path) -> LeadRadarConfig:
    scoring_raw = _load_yaml(config_dir / "scoring.yaml")
    sources_raw = _load_yaml(config_dir / "sources.yaml")
    packs_raw = _load_yaml(config_dir / "keyword_packs.yaml")

    time_zone = str(scoring_raw.get("time_zone") or scoring_raw.get("timezone") or "UTC").strip() or "UTC"
    scoring = ScoringConfig(
        time_zone=time_zone,
        window_days=int(scoring_raw["window_days"]),
        half_life_days=int(scoring_raw["half_life_days"]),
        min_signal_confidence=float(scoring_raw["min_signal_confidence"]),
        promote_threshold=float(scoring_raw["promote_threshold"]),
    )

    sources: list[Source] = []
    for src in sources_raw.get("sources", []):
        sources.append(
            Source(
                id=str(src["id"]),
                enabled=bool(src.get("enabled", True)),
                type=str(src.get("type", "rss")),
                name=str(src.get("name", src["id"])),
                url=str(src["url"]),
                tags=[str(t) for t in (src.get("tags") or [])],
                weight=float(src.get("weight", 1.0) or 1.0),
                max_items=int(src.get("max_items", 20) or 20),
                include_regex=str(src.get("include_regex")).strip()
                if src.get("include_regex") is not None and str(src.get("include_regex")).strip()
                else None,
                exclude_regex=str(src.get("exclude_regex")).strip()
                if src.get("exclude_regex") is not None and str(src.get("exclude_regex")).strip()
                else None,
            )
        )

    packs: list[KeywordPack] = []
    for pack in packs_raw.get("packs", []):
        keywords = [
            Keyword(keyword=str(k["keyword"]), weight=float(k["weight"]))
            for k in (pack.get("keywords") or [])
        ]
        packs.append(
            KeywordPack(
                id=str(pack["id"]),
                enabled=bool(pack.get("enabled", True)),
                name=str(pack.get("name", pack["id"])),
                tags=[str(t) for t in (pack.get("tags") or [])],
                keywords=keywords,
            )
        )

    return LeadRadarConfig(scoring=scoring, sources=sources, keyword_packs=packs)
