from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass


_ORG_SUFFIXES = [
    "High School",
    "School",
    "Academy",
    "College",
    "University",
    "District",
    "Hospital",
    "Ministry",
    "Inc",
    "Inc.",
    "LLC",
    "Ltd",
    "Ltd.",
    "PLC",
    "Corp",
    "Corp.",
    "Corporation",
    "Company",
]

_ORG_SUFFIX_PATTERN = "|".join(re.escape(s) for s in sorted(_ORG_SUFFIXES, key=len, reverse=True))

_EXPLICIT_ORG_RE = re.compile(r"\b(?:Organization|Source|Sources)\s*:\s*([^\n<]+)", flags=re.IGNORECASE)

_ORG_RE = re.compile(
    rf"(?P<name>\b[A-Z][A-Za-z0-9&'\-]+(?:\s+[A-Z][A-Za-z0-9&'\-]+){{0,6}})\s+(?P<suffix>{_ORG_SUFFIX_PATTERN})\b"
)


@dataclass(frozen=True)
class OrgCandidate:
    name: str
    confidence: float


def normalize_org_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


def extract_org_candidate(text: str) -> OrgCandidate | None:
    if not text:
        return None
    decoded = html_lib.unescape(text)

    m = _EXPLICIT_ORG_RE.search(decoded)
    if m:
        value = re.sub(r"\s+", " ", m.group(1).strip())
        if "," in value:
            value = value.split(",", 1)[0].strip()
        if value:
            return OrgCandidate(name=value, confidence=0.9)

    match = _ORG_RE.search(decoded)
    if not match:
        return None

    name = f"{match.group('name').strip()} {match.group('suffix').strip()}"
    suffix = match.group("suffix").lower()

    tokens = name.split()
    if tokens and tokens[0].lower() in {"how", "what", "why"}:
        return None
    if tokens and tokens[0].lower() == "the" and len(tokens) <= 2:
        return None

    if "school" in suffix or "district" in suffix or "university" in suffix or "college" in suffix:
        confidence = 0.85
    elif suffix in {"inc", "inc.", "llc", "ltd", "ltd.", "plc", "corp", "corp.", "corporation"}:
        confidence = 0.8
    else:
        confidence = 0.7

    return OrgCandidate(name=name, confidence=confidence)
