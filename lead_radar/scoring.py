from __future__ import annotations

import math
from datetime import datetime, timezone


def decay_multiplier(half_life_days: int, now: datetime, ts: datetime) -> float:
    if half_life_days <= 0:
        return 1.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    lam = math.log(2.0) / float(half_life_days)
    return math.exp(-lam * age_days)


def score_to_percent(raw_score: float, *, scale: float = 16.0) -> float:
    """
    Convert an unbounded aggregate score into a 0-100 score.

    We use a saturating exponential curve so early signal accumulation is
    visible (useful for MVP lead discovery) while still reserving 100 for
    very strong / repeated intent.
    """
    raw = float(raw_score or 0.0)
    if raw <= 0.0:
        return 0.0
    k = float(scale or 16.0)
    if k <= 0:
        return min(100.0, raw)
    pct = 100.0 * (1.0 - math.exp(-raw / k))
    return max(0.0, min(100.0, pct))
