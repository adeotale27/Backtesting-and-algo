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


def test_paper_any_day_watches_on_non_expiry(tmp_path):
    """Paper + paper_any_day streams both indexes even when calendar is empty."""
    from cas_rule_expiry_automation.expiry_calendar import today_indexes
    from unittest.mock import patch

    cfg = _cfg(tmp_path)
    cfg.live_trading = False
    cfg.paper_any_day = True
    wed = datetime(2026, 8, 5, 12, 0, tzinfo=IST)  # Wednesday — no expiry
    with patch("cas_rule_expiry_automation.expiry_calendar.get_ist_now", return_value=wed):
        assert today_indexes(cfg, wed) == ["NIFTY", "SENSEX"]
        day = describe_today(cfg, wed)
        assert day["is_expiry_day"] is False
        assert day["indexes"] == ["NIFTY", "SENSEX"]
        assert day["paper_any_day"] is True

    # LIVE money still respects calendar
    cfg.live_trading = True
    assert today_indexes(cfg, wed) == []


def test_backtest_models_market_ack_latency(tmp_path):
    """Backtest detect→sell is not instant — models fill_latency_ms like live ack."""
    cfg = _cfg(tmp_path)
    cfg.fill_latency_ms = 12.0
    result = run_ws_backtest(
        kite=None,
        config=cfg,
        start=date(2026, 8, 6),
        end=date(2026, 8, 6),
        capital=500_000,
        lots=1,
        close_overrides={"2026-08-06:SENSEX": 78954.76},
    )
    assert result.num_trades == 1
    t = result.trades[0]
    assert t["detect_to_ce_ms"] >= 12.0
    assert t["detect_to_pe_ms"] >= t["detect_to_ce_ms"]
    assert t["detect_to_done_ms"] >= 12.0
    assert result.avg_detect_to_done_ms >= 12.0


def test_nearest_expiry_prefix_fallback():
    """Non-expiry day: use nearest upcoming weekly contracts for strike resolve."""
    from cas_rule_expiry_automation.strike_resolver import detect_expiry_prefix
    from datetime import date as d

    class FakeKite:
        def instruments(self, exchange):
            return [
                {
                    "tradingsymbol": "SENSEX2681379000CE",
                    "expiry": d(2026, 8, 13),
                    "instrument_type": "CE",
                },
                {
                    "tradingsymbol": "SENSEX2681378900PE",
                    "expiry": d(2026, 8, 13),
                    "instrument_type": "PE",
                },
            ]

    # Wednesday 2026-08-05 — no contracts expire that day
    prefix = detect_expiry_prefix(FakeKite(), "SENSEX", on_date=d(2026, 8, 5))
    assert prefix and prefix.startswith("SENSEX")


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
    now = datetime(2026, 8, 6, 15, 27, tzinfo=IST)
    assert in_window(now, time(15, 27), time(15, 35))
    now2 = datetime(2026, 8, 6, 15, 28, tzinfo=IST)
    assert in_window(now2, time(15, 27), time(15, 35))
    early = datetime(2026, 8, 6, 15, 26, tzinfo=IST)
    assert not in_window(early, time(15, 27), time(15, 35))


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
        assert t["detect_to_done_ms"] >= 8.0
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
    assert t["quantity"] == 3 * 20  # Sensex lot — per leg
    assert "15:29:30" in t["cas_detected_at"]
    assert t["ce_sold_at"]
    assert t["pe_sold_at"]
    assert t["detect_to_ce_ms"] >= 0
    assert t["detect_to_pe_ms"] >= 0
    assert "total_decay" in t
    assert t["total_decay"] == t["pnl"]

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


def test_infer_cas_detect_and_premium_uses_minute_close():
    from cas_rule_expiry_automation.backtest_ws import (
        _infer_cas_detect_ts,
        _premium_from_bars,
    )

    d = date(2026, 8, 6)
    candles = [
        {
            "date": datetime(2026, 8, 6, 15, 28, tzinfo=IST),
            "open": 78785.62,
            "high": 78785.62,
            "low": 78785.62,
            "close": 78785.62,
        },
        {
            "date": datetime(2026, 8, 6, 15, 29, tzinfo=IST),
            "open": 78785.62,
            "high": 78954.76,
            "low": 78785.62,
            "close": 78954.76,
        },
    ]
    ts, src = _infer_cas_detect_ts(candles, 78954.76, d)
    assert ts.hour == 15 and ts.minute == 29 and ts.second == 30
    assert src == "kite_cas_bar"

    bars = [
        {
            "date": datetime(2026, 8, 6, 15, 28, tzinfo=IST),
            "open": 79.35,
            "high": 110.3,
            "low": 73.9,
            "close": 102.3,
        },
        {
            "date": datetime(2026, 8, 6, 15, 29, tzinfo=IST),
            "open": 102.3,
            "high": 104.9,
            "low": 0.7,
            "close": 1.2,
        },
        {
            "date": datetime(2026, 8, 6, 15, 35, tzinfo=IST),
            "open": 0.3,
            "high": 0.3,
            "low": 0.15,
            "close": 0.15,
        },
    ]
    entry, exit_px, detail = _premium_from_bars(bars, ts)
    # Entry = last live close BEFORE CAS minute (15:28 = 102.3), NOT collapsed 1.2
    assert entry == 102.3
    assert exit_px == 0.15
    assert "pre_cas_close=102.30" in detail
    assert "cas_min@15:29" in detail


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


def test_parallel_market_sell_both_legs(tmp_path):
    """Live fire path: both legs MARKET SELL without waiting sequentially."""
    from cas_rule_expiry_automation.order_engine import OrderEngine
    from cas_rule_expiry_automation.strike_resolver import Leg
    from cas_rule_expiry_automation.timing import new_detect_event
    import time

    class FakeClient:
        def __init__(self):
            self.calls = []

        def place_market_sell(self, **kwargs):
            self.calls.append(kwargs)
            time.sleep(0.01)  # simulate RTT
            return f"OID-{kwargs['tradingsymbol']}"

    client = FakeClient()
    eng = OrderEngine(client, lots=2, product="NRML", live_trading=True)
    legs = [
        Leg("SENSEX", "CE", 79000, "SENSEX2680679000CE", "BFO", 1, 20),
        Leg("SENSEX", "PE", 78900, "SENSEX2680678900PE", "BFO", 2, 20),
    ]
    timing = new_detect_event("SENSEX", 78954.76, "test", source="test")
    t0 = time.perf_counter()
    fills, timing = eng.sell_otm(legs, 78954.76, "test", t0, timing=timing)
    elapsed = (time.perf_counter() - t0) * 1000
    assert len(fills) == 2
    assert {f.opt_type for f in fills} == {"CE", "PE"}
    assert all(f.quantity == 40 for f in fills)  # 2 lots × 20
    assert all("MARKET" in f.trigger for f in fills)
    assert all(f.price == 0.0 for f in fills)  # MARKET — no limit price
    assert len(client.calls) == 2
    assert all(c.get("live") is True for c in client.calls)
    assert timing.ce_sold_at and timing.pe_sold_at
    # Parallel: wall time ~ one RTT, not two sequential RTTs
    assert elapsed < 25, f"expected parallel ~10ms, got {elapsed:.1f}ms"


def test_kite_place_market_sell_never_limit():
    """Kite MARKET sell: no price/trigger_price; market_protection=-1 required."""
    from cas_rule_expiry_automation.kite_client import KiteClient
    from types import SimpleNamespace

    captured = {}

    class FakeKite:
        VARIETY_REGULAR = "regular"
        TRANSACTION_TYPE_SELL = "SELL"
        ORDER_TYPE_MARKET = "MARKET"
        ORDER_TYPE_LIMIT = "LIMIT"
        VALIDITY_DAY = "DAY"
        MARKET_PROTECTION_AUTO = -1

        def place_order(self, **kwargs):
            captured.update(kwargs)
            return "OID-1"

    client = KiteClient.__new__(KiteClient)
    client.config = SimpleNamespace()
    client.kite = FakeKite()
    oid = client.place_market_sell(
        exchange="BFO",
        tradingsymbol="SENSEX2680679000CE",
        quantity=20,
        product="NRML",
        live=True,
    )
    assert oid == "OID-1"
    assert captured["order_type"] == "MARKET"
    assert captured["order_type"] != "LIMIT"
    assert captured["transaction_type"] == "SELL"
    assert captured["quantity"] == 20
    # price / trigger_price must be omitted — SDK strips None; 0 would be sent
    assert "price" not in captured
    assert "trigger_price" not in captured
    # Zerodha rejects unprotected MARKET (protection=0); AUTO=-1 is required
    assert captured["market_protection"] == -1


def test_ws_heartbeat_does_not_persist(tmp_path):
    """Heartbeat must not rewrite runtime_state.json (blocks fire path)."""
    from cas_rule_expiry_automation.state import StateStore
    import os

    path = tmp_path / "runtime_state.json"
    store = StateStore(str(path))
    store.activate("test")
    mtime1 = os.path.getmtime(path)
    store.set_ws(True, ticks=42)
    store.set_ltp("SENSEX", 79000.5)
    mtime2 = os.path.getmtime(path)
    assert mtime1 == mtime2
    assert store.snapshot()["ws_connected"] is True
    assert store.snapshot()["ticks_seen"] == 42
    assert store.snapshot()["last_ltp"]["SENSEX"] == 79000.5


def test_watch_start_migrates_1528_to_1527(tmp_path):
    from cas_rule_expiry_automation.config import load_config

    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[cas_window]\nwatch_start = 15:28:00\nwatch_end = 15:35:00\n"
        "[kite]\napi_key = x\napi_secret = y\naccess_token =\n"
        "[admin]\nusername = a\npassword = b\n"
        "[strategy]\nlots = 1\n"
        "[latency]\nws_mode = full\n"
        "[safety]\nlive_trading = false\n"
        "[server]\nhost = 127.0.0.1\nport = 5030\n"
        "[backtest]\ndefault_capital = 500000\n"
    )
    cfg = load_config(str(cfg_path))
    assert cfg.watch_start.hour == 15 and cfg.watch_start.minute == 27
    # Persisted
    text = cfg_path.read_text()
    assert "15:27:00" in text
    assert "15:28:00" not in text


def test_backtest_page_defaults_to_today(tmp_path):
    from cas_rule_expiry_automation.config import ensure_config
    from cas_rule_expiry_automation.app import app
    from cas_rule_expiry_automation.time_utils import get_ist_now

    ensure_config()
    today = get_ist_now().date().isoformat()
    c = app.test_client()
    with c.session_transaction() as s:
        s["ok"] = True
        s["user"] = "admin"
    bt = c.get("/backtest")
    assert bt.status_code == 200
    assert today.encode() in bt.data
    assert b"setMonth" not in bt.data
    assert b"WebSocket backtest" not in bt.data


def test_activate_deactivate_idempotent(tmp_path):
    from cas_rule_expiry_automation.state import StateStore

    store = StateStore(str(tmp_path / "state.json"))
    a1 = store.activate("u")
    assert a1["activated"] is True and a1.get("unchanged") is False
    a2 = store.activate("u")
    assert a2["activated"] is True and a2.get("unchanged") is True
    # Only one activated event
    kinds = [e["kind"] for e in store.snapshot()["events"]]
    assert kinds.count("activated") == 1

    d1 = store.deactivate("u")
    assert d1["activated"] is False and d1.get("unchanged") is False
    d2 = store.deactivate("u")
    assert d2["activated"] is False and d2.get("unchanged") is True
    kinds2 = [e["kind"] for e in store.snapshot()["events"]]
    assert kinds2.count("deactivated") == 1

    # Persist across new store instance (page refresh)
    store.activate("u")
    store2 = StateStore(str(tmp_path / "state.json"))
    assert store2.is_activated() is True


def test_app_login_page(tmp_path):
    # Point config via ensuring package config exists from example
    from cas_rule_expiry_automation.config import ensure_config
    from cas_rule_expiry_automation.app import app

    ensure_config()
    c = app.test_client()
    assert c.get("/login").status_code == 200
    with c.session_transaction() as s:
        s["ok"] = True
        s["user"] = "admin"
    live = c.get("/")
    assert live.status_code == 200
    assert b"Activate CAS window" in live.data
    assert b"Deactivate CAS window" in live.data
    assert b"15:27" in live.data
    bt = c.get("/backtest")
    assert bt.status_code == 200
    assert b"Run backtest" in bt.data
    assert b"WebSocket backtest" not in bt.data
    assert b"Backtest" in bt.data

    # Activate once → button state reflected; second activate is unchanged
    r1 = c.post("/api/activate", json={})
    assert r1.status_code == 200
    assert r1.get_json()["unchanged"] is False
    r2 = c.post("/api/activate", json={})
    assert r2.get_json()["unchanged"] is True
    assert r2.get_json()["state"]["activated"] is True
    live2 = c.get("/")
    assert b"disabled" in live2.data
    r3 = c.post("/api/deactivate", json={})
    assert r3.get_json()["unchanged"] is False
    r4 = c.post("/api/deactivate", json={})
    assert r4.get_json()["unchanged"] is True
