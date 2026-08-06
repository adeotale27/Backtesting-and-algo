"""Tests for CAS Rule Expiry Automation."""

from __future__ import annotations

import os
import shutil
from datetime import date, datetime, time, timezone, timedelta

from cas_rule_expiry_automation.backtest_ws import run_ws_backtest
from cas_rule_expiry_automation.config import load_config
from cas_rule_expiry_automation.expiry_calendar import indexes_for_date, describe_today
from cas_rule_expiry_automation.strike_resolver import otm_strikes, round_atm
from cas_rule_expiry_automation.time_utils import in_window
from cas_rule_expiry_automation.ws_stream import TickBus, TickReplay, candle_to_ticks

IST = timezone(timedelta(hours=5, minutes=30))


def _cfg(tmp_path):
    example = os.path.join(
        os.path.dirname(__file__), "..", "config.ini.example"
    )
    dest = tmp_path / "config.ini"
    shutil.copy(example, dest)
    return load_config(str(dest))


def test_expiry_calendar_tue_thu(tmp_path):
    cfg = _cfg(tmp_path)
    # 2026-08-04 is Tuesday, 2026-08-06 is Thursday
    assert indexes_for_date(date(2026, 8, 4), cfg) == ["NIFTY"]
    assert indexes_for_date(date(2026, 8, 6), cfg) == ["SENSEX"]
    assert indexes_for_date(date(2026, 8, 5), cfg) == []


def test_otm_strikes():
    atm, ce, pe = otm_strikes(24850, 50, 1, 1)
    assert atm == 24850 and ce == 24900 and pe == 24800
    atm, ce, pe = otm_strikes(81100, 100, 2, 2)
    assert ce == 81300 and pe == 80900


def test_round_atm():
    assert round_atm(24837, 50) == 24850


def test_in_window():
    now = datetime(2026, 8, 6, 15, 29, tzinfo=IST)
    assert in_window(now, time(15, 28), time(15, 35))


def test_tick_bus_and_replay():
    bus = TickBus()
    seen = []
    bus.add_handler(lambda ticks: seen.extend(ticks))
    candles = [
        {"date": datetime(2026, 8, 4, 15, 0), "open": 100, "high": 101, "low": 99, "close": 100.5},
        {"date": datetime(2026, 8, 4, 15, 1), "open": 100.5, "high": 102, "low": 100, "close": 101},
    ]
    ticks = candle_to_ticks(256265, candles, ticks_per_candle=2)
    assert ticks[-1].get("cas_close") is True
    n = TickReplay(bus).run(ticks, interval_ms=0)
    assert n == len(ticks)
    assert len(seen) == n
    assert bus.stats.ticks_received == n


def test_ws_backtest_synthetic(tmp_path):
    cfg = _cfg(tmp_path)
    result = run_ws_backtest(
        kite=None,
        config=cfg,
        start=date(2026, 5, 1),
        end=date(2026, 6, 30),
        capital=500_000,
    )
    assert result.num_trades > 0
    assert result.ws_ticks_total > 0
    # Only Tue/Thu should appear
    indexes = {t["index"] for t in result.trades}
    assert indexes <= {"NIFTY", "SENSEX"}
    for t in result.trades:
        d = date.fromisoformat(t["entry_date"])
        if t["index"] == "NIFTY":
            assert d.weekday() == 1
        if t["index"] == "SENSEX":
            assert d.weekday() == 3


def test_configurable_lots_in_config(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.lots == 1
    assert cfg.ce_otm_steps == 1
    assert cfg.pe_otm_steps == 1


def test_app_login_page(tmp_path):
    # Point config via ensuring package config exists from example
    from cas_rule_expiry_automation.config import ensure_config
    from cas_rule_expiry_automation.app import app

    ensure_config()
    c = app.test_client()
    assert c.get("/login").status_code == 200
