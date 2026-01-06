from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentHit:
    rule_id: str
    weight: float
    label: str


_DEFAULT_RULES: list[tuple[str, float, str, re.Pattern]] = [
    (
        "procurement_rfp",
        18.0,
        "RFP / tender / bid",
        re.compile(r"\b(request for proposal|rfp|tender|bid solicitation|sealed bid)\b", re.IGNORECASE),
    ),
    (
        "procurement_vendor",
        12.0,
        "Seeking vendor / supplier",
        re.compile(r"\b(seeking (a )?vendor|looking for (a )?vendor|supplier|vendor selection)\b", re.IGNORECASE),
    ),
    (
        "implementation_project",
        8.0,
        "Implementation / migration project",
        re.compile(r"\b(implementation|deploy(ment)?|migration|rollout|upgrade project)\b", re.IGNORECASE),
    ),
    (
        "job_it_admin",
        6.0,
        "Hiring IT admin / engineer",
        re.compile(r"\b(hiring|job opening|vacancy)\b.{0,80}\b(it|sysadmin|system administrator|network|security)\b", re.IGNORECASE),
    ),
]


def match_intent(text: str) -> list[IntentHit]:
    if not text:
        return []
    hits: list[IntentHit] = []
    for rule_id, weight, label, pattern in _DEFAULT_RULES:
        if pattern.search(text):
            hits.append(IntentHit(rule_id=rule_id, weight=weight, label=label))
    return hits

