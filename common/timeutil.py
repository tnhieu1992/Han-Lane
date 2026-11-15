"""Time helpers. Wall clock is time.time_ns(); stored times are tz-aware BSON dates."""
from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ns() -> int:
    return time.time_ns()


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc)


def ms_between(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1e6


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
