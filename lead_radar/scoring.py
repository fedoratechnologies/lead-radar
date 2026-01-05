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

