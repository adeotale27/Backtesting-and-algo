"""IST time helpers for the CAS Expiry algo."""

from __future__ import annotations

from datetime import datetime, time, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now() -> datetime:
    """Return timezone-aware current time in IST."""
    return datetime.now(IST)


def in_window(now: datetime, start: time, end: time) -> bool:
    """True if ``now.time()`` falls within [start, end] inclusive."""
    t = now.timetz().replace(tzinfo=None) if now.tzinfo else now.time()
    # Compare as naive times for simplicity
    nt = time(t.hour, t.minute, t.second, t.microsecond)
    return start <= nt <= end
