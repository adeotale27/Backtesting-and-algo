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
    # Exact ATM → classic wings
    atm, ce, pe = otm_strikes(24850, 50, 1, 1)
    assert atm == 24850 and ce == 24900 and pe == 24800
    atm, ce, pe = otm_strikes(81100, 100, 2, 2)
    assert ce == 81300 and pe == 80900

    # Spot below ATM (Sensex 06-Aug-2026 style): sell ATM CE + (ATM−1) PE
    atm, ce, pe = otm_strikes(78954.76, 100, 1, 1)
    assert atm == 79000 and ce == 79000 and pe == 78900

    # Spot above ATM: sell (ATM+1) CE + ATM PE
    atm, ce, pe = otm_strikes(79022, 100, 1, 1)
    assert atm == 79000 and ce == 79100 and pe == 79000

    # Nifty spot below ATM
    atm, ce, pe = otm_strikes(24837, 50, 1, 1)
    assert atm == 24850 and ce == 24850 and pe == 24800


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
    assert result.timings
    indexes = {t["index"] for t in result.trades}
    assert indexes <= {"NIFTY", "SENSEX"}
    for t in result.trades:
        d = date.fromisoformat(t["entry_date"])
        if t["index"] == "NIFTY":
            assert d.weekday() == 1
        if t["index"] == "SENSEX":
            assert d.weekday() == 3
        # Timing stamps present
        assert t["cas_detected_at"]
        assert "15:28" in t["cas_detected_at"] or "15:29" in t["cas_detected_at"]
        assert t["ce_sold_at"]
        assert t["pe_sold_at"]
        assert t["detect_to_done_ms"] >= 0
        assert t["ce_strike"] > t["atm"] or t["ce_strike"] == t["atm"]
        assert t["pe_strike"] < t["atm"] or t["pe_strike"] == t["atm"]
        assert t["ce_premium"] >= 0
        assert t["pe_premium"] >= 0
        assert t["lots"] >= 1
        assert t["data_source"] == "synthetic"


def test_ws_backtest_lots_and_sensex_strike_bias(tmp_path):
    cfg = _cfg(tmp_path)
    result = run_ws_backtest(
        kite=None,
        config=cfg,
        start=date(2026, 8, 6),
        end=date(2026, 8, 6),
        capital=500_000,
        lots=3,
        close_overrides={"2026-08-06:SENSEX": 78954.76},
    )
    assert result.num_trades == 1
    t = result.trades[0]
    assert t["index"] == "SENSEX"
    assert t["close_price"] == 78954.76
    assert t["atm"] == 79000
    assert t["ce_strike"] == 79000  # ATM CE when spot < ATM
    assert t["pe_strike"] == 78900
    assert t["lots"] == 3
    assert t["quantity"] == 3 * 20  # Sensex lot
    assert t["cas_detected_at"]
    assert t["ce_sold_at"]
    assert t["pe_sold_at"]
    assert t["detect_to_ce_ms"] >= 0
    assert t["detect_to_pe_ms"] >= 0

    # Spot above ATM → ATM PE
    result2 = run_ws_backtest(
        kite=None,
        config=cfg,
        start=date(2026, 8, 6),
        end=date(2026, 8, 6),
        capital=500_000,
        lots=1,
        close_overrides={"2026-08-06:SENSEX": 79022},
    )
    t2 = result2.trades[0]
    assert t2["ce_strike"] == 79100
    assert t2["pe_strike"] == 79000


def test_cas_premium_bs_fallback_no_floor():
    from cas_rule_expiry_automation.backtest_ws import cas_entry_premium

    # With floor=0, premium is pure BS (can be small near expiry) — not forced to 100
    ce = cas_entry_premium(78954.76, 79100, "CE", 100, 35.0, 15.0, 0.0)
    assert ce >= 0
    assert ce < 100  # should NOT be the old hardcoded floor


def test_timing_ms_between():
    from cas_rule_expiry_automation.timing import ms_between, new_detect_event

    a = "2026-08-06T15:28:41.100+05:30"
    b = "2026-08-06T15:28:41.250+05:30"
    assert abs(ms_between(a, b) - 150) < 0.01
    ev = new_detect_event("NIFTY", 24850, "ws_ohlc_close", detected_at=a)
    assert ev.cas_detected_at == a
    assert ev.index == "NIFTY"


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
