"""IST helpers."""

from __future__ import annotations

from datetime import datetime, time, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now() -> datetime:
    return datetime.now(IST)


def time_only(dt: datetime) -> time:
    t = dt.timetz().replace(tzinfo=None) if dt.tzinfo else dt.time()
    return time(t.hour, t.minute, t.second, t.microsecond)


def in_window(now: datetime, start: time, end: time) -> bool:
    return start <= time_only(now) <= end
