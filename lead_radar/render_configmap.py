from __future__ import annotations

from typing import Any

import yaml


def _dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()


def _indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join((prefix + line) if line else "" for line in text.splitlines())


def render_lead_radar_configmap(
    scoring: dict[str, Any],
    sources: list[dict[str, Any]],
    keyword_packs: list[dict[str, Any]],
) -> str:
    scoring_block = _indent(_dump_yaml(scoring), 4)
    sources_block = _indent(_dump_yaml({"sources": sources}), 4)
    packs_block = _indent(_dump_yaml({"packs": keyword_packs}), 4)

    parts = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: lead-radar-config",
        "  namespace: lead-radar",
        "  annotations:",
        '    argocd.argoproj.io/sync-wave: "1"',
        "data:",
        "  scoring.yaml: |",
        scoring_block,
        "  sources.yaml: |",
        sources_block,
        "  keyword_packs.yaml: |",
        packs_block,
        "",
    ]
    return "\n".join(parts)
