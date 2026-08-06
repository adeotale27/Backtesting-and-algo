"""Weekly expiry calendar — Tuesday NIFTY, Thursday SENSEX."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

from cas_rule_expiry_automation.config import AppConfig
from cas_rule_expiry_automation.time_utils import get_ist_now


@dataclass(frozen=True)
class ExpiryDay:
    index: str
    weekday_name: str
    date: date


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

INDEX_META = {
    "NIFTY": {
        "token": 256265,
        "spot_key": "NSE:NIFTY 50",
        "exchange": "NFO",
        "segment": "NFO-OPT",
        "strike_gap": 50,
        "name": "NIFTY",
        "default_lot": 65,
    },
    "SENSEX": {
        "token": 265,
        "spot_key": "BSE:SENSEX",
        "exchange": "BFO",
        "segment": "BFO-OPT",
        "strike_gap": 100,
        "name": "SENSEX",
        "default_lot": 20,
    },
}


def indexes_for_date(d: date, cfg: AppConfig) -> List[str]:
    """Return which underlyings to trade on date ``d`` (strict expiry calendar)."""
    if not cfg.expiry_only:
        return ["NIFTY", "SENSEX"]
    out: List[str] = []
    if d.weekday() == cfg.nifty_expiry_weekday:
        out.append("NIFTY")
    if d.weekday() == cfg.sensex_expiry_weekday:
        out.append("SENSEX")
    return out


def today_indexes(cfg: AppConfig, now: Optional[datetime] = None) -> List[str]:
    """Indexes to watch today.

    Live money: only weekly expiry underlyings (unless expiry_only=false).
    Paper + paper_any_day: if today is not an expiry, still watch both so you
    can verify WebSocket → detect → dry MARKET path with real Kite ticks.
    """
    now = now or get_ist_now()
    indexes = indexes_for_date(now.date(), cfg)
    if indexes:
        return indexes
    if (not cfg.live_trading) and getattr(cfg, "paper_any_day", True):
        return ["NIFTY", "SENSEX"]
    return []


def describe_today(cfg: AppConfig, now: Optional[datetime] = None) -> dict:
    now = now or get_ist_now()
    d = now.date()
    calendar = indexes_for_date(d, cfg)
    indexes = today_indexes(cfg, now)
    return {
        "date": d.isoformat(),
        "weekday": WEEKDAY_NAMES[d.weekday()],
        "weekday_num": d.weekday(),
        "is_expiry_day": bool(calendar),
        "indexes": indexes,
        "calendar_indexes": calendar,
        "paper_any_day": bool(getattr(cfg, "paper_any_day", True)) and not cfg.live_trading,
        "nifty_on": WEEKDAY_NAMES[cfg.nifty_expiry_weekday],
        "sensex_on": WEEKDAY_NAMES[cfg.sensex_expiry_weekday],
    }


def next_expiry_dates(cfg: AppConfig, from_date: Optional[date] = None, count: int = 8) -> List[ExpiryDay]:
    """Upcoming Tue/Thu expiry slots."""
    from datetime import timedelta

    d = from_date or get_ist_now().date()
    out: List[ExpiryDay] = []
    for _ in range(60):
        if d.weekday() == cfg.nifty_expiry_weekday:
            out.append(ExpiryDay("NIFTY", WEEKDAY_NAMES[d.weekday()], d))
        if d.weekday() == cfg.sensex_expiry_weekday:
            out.append(ExpiryDay("SENSEX", WEEKDAY_NAMES[d.weekday()], d))
        if len(out) >= count:
            break
        d += timedelta(days=1)
    return out[:count]
