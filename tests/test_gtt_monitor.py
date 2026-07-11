"""Unit tests for gtt_monitor.py business logic.

Tests pure functions (no live API, no Flask). Covers:
  - build_nfo_nrml_positions_map filtering
  - build_pending_nfo_gtt_map filtering and aggregation
  - find_mismatches both alert conditions
  - run_gtt_monitor_check deduplication (alert fires only for new mismatches)
"""

import sys
import os

# Ensure the vibhu package root is on the path so gtt_monitor imports cleanly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import gtt_monitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos(symbol: str, quantity: int, exchange: str = "NFO", product: str = "NRML") -> dict:
    return {"tradingsymbol": symbol, "quantity": quantity, "exchange": exchange, "product": product}


def _gtt(symbol: str, qty: int, side: str = "BUY", exchange: str = "NFO", result=None) -> dict:
    return {
        "orders": [
            {
                "tradingsymbol": symbol,
                "quantity": qty,
                "transaction_type": side,
                "exchange": exchange,
                "result": result,
            }
        ]
    }


# ---------------------------------------------------------------------------
# build_nfo_nrml_positions_map
# ---------------------------------------------------------------------------


def test_positions_map_filters_non_nfo():
    positions = [
        _pos("NIFTY24DEC18000PE", -50, exchange="NFO"),
        _pos("RELIANCE", 100, exchange="NSE"),
    ]
    result = gtt_monitor.build_nfo_nrml_positions_map(positions)
    assert "NIFTY24DEC18000PE" in result
    assert "RELIANCE" not in result


def test_positions_map_filters_non_nrml():
    positions = [
        _pos("NIFTY24DEC18000PE", -50, product="NRML"),
        _pos("BANKNIFTY24DEC45000CE", -25, product="MIS"),
    ]
    result = gtt_monitor.build_nfo_nrml_positions_map(positions)
    assert "NIFTY24DEC18000PE" in result
    assert "BANKNIFTY24DEC45000CE" not in result


def test_positions_map_preserves_negative_qty():
    positions = [_pos("NIFTY24DEC18000PE", -50)]
    result = gtt_monitor.build_nfo_nrml_positions_map(positions)
    assert result["NIFTY24DEC18000PE"] == -50


# ---------------------------------------------------------------------------
# build_pending_nfo_gtt_map
# ---------------------------------------------------------------------------


def test_gtt_map_filters_executed():
    gtts = [
        _gtt("NIFTY24DEC18000PE", 50, result={"status": "COMPLETE"}),
    ]
    result = gtt_monitor.build_pending_nfo_gtt_map(gtts)
    assert "NIFTY24DEC18000PE" not in result


def test_gtt_map_filters_non_nfo():
    gtts = [
        _gtt("RELIANCE", 10, exchange="NSE"),
    ]
    result = gtt_monitor.build_pending_nfo_gtt_map(gtts)
    assert "RELIANCE" not in result


def test_gtt_map_aggregates_multiple_buy_gtts():
    gtts = [
        _gtt("NIFTY24DEC18000PE", 50, side="BUY"),
        _gtt("NIFTY24DEC18000PE", 25, side="BUY"),
    ]
    result = gtt_monitor.build_pending_nfo_gtt_map(gtts)
    assert result["NIFTY24DEC18000PE"]["BUY"] == 75


def test_gtt_map_tracks_sell_separately():
    gtts = [
        _gtt("NIFTY24DEC18000PE", 50, side="BUY"),
        _gtt("NIFTY24DEC18000PE", 30, side="SELL"),
    ]
    result = gtt_monitor.build_pending_nfo_gtt_map(gtts)
    assert result["NIFTY24DEC18000PE"]["BUY"] == 50
    assert result["NIFTY24DEC18000PE"]["SELL"] == 30


# ---------------------------------------------------------------------------
# find_mismatches
# ---------------------------------------------------------------------------


def test_no_mismatch_when_buy_covers_short_exactly():
    # short 50 + buy 50 = 0 → not a net long → no alert
    positions_map = {"NIFTY24DEC18000PE": -50}
    gtt_map = {"NIFTY24DEC18000PE": {"BUY": 50, "SELL": 0}}
    alerts = gtt_monitor.find_mismatches(positions_map, gtt_map)
    assert alerts == []


def test_gtt_position_excess_when_buy_exceeds_short():
    # short 50 + buy 100 = +50 → would go LONG
    positions_map = {"NIFTY24DEC18000PE": -50}
    gtt_map = {"NIFTY24DEC18000PE": {"BUY": 100, "SELL": 0}}
    alerts = gtt_monitor.find_mismatches(positions_map, gtt_map)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["type"] == "GTT_POSITION_EXCESS"
    assert alert["symbol"] == "NIFTY24DEC18000PE"
    assert alert["position_qty"] == -50
    assert alert["gtt_buy_qty"] == 100
    assert alert["net_if_triggered"] == 50


def test_gtt_orphaned_when_position_is_flat():
    positions_map: dict = {}
    gtt_map = {"NIFTY24DEC18000PE": {"BUY": 50, "SELL": 0}}
    alerts = gtt_monitor.find_mismatches(positions_map, gtt_map)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["type"] == "GTT_ORPHANED"
    assert alert["symbol"] == "NIFTY24DEC18000PE"
    assert alert["gtt_buy_qty"] == 50
    assert alert["position_qty"] == 0


def test_sell_only_gtt_does_not_trigger_orphaned():
    positions_map: dict = {}
    gtt_map = {"NIFTY24DEC18000PE": {"BUY": 0, "SELL": 50}}
    alerts = gtt_monitor.find_mismatches(positions_map, gtt_map)
    assert alerts == []


def test_multiple_alerts_returned():
    positions_map = {"NIFTY24DEC18000PE": -50}
    gtt_map = {
        "NIFTY24DEC18000PE": {"BUY": 100, "SELL": 0},  # EXCESS
        "BANKNIFTY24DEC45000CE": {"BUY": 25, "SELL": 0},  # ORPHANED
    }
    alerts = gtt_monitor.find_mismatches(positions_map, gtt_map)
    types = {a["type"] for a in alerts}
    assert "GTT_POSITION_EXCESS" in types
    assert "GTT_ORPHANED" in types


# ---------------------------------------------------------------------------
# Deduplication via run_gtt_monitor_check
# ---------------------------------------------------------------------------


def _reset_monitor_state():
    """Reset module-level state between dedup tests."""
    gtt_monitor._known_mismatches = set()
    gtt_monitor._last_checked_at = None
    gtt_monitor._last_mismatch_list = []


def _make_fake_kite(positions_net: list, gtts: list):
    """Return a minimal fake KiteConnect object for dedup tests."""

    class _FakeKite:
        def positions(self):
            return {"net": positions_net}

        def get_gtts(self):
            return gtts

    return _FakeKite()


def test_dedup_no_double_notification(monkeypatch):
    """Same mismatch on two consecutive checks → notification fires only once."""
    _reset_monitor_state()

    dispatched: list[str] = []

    def _fake_dispatch(notif_type, title="", body="", metadata=None):
        dispatched.append(notif_type)
        return 1

    import types as _types

    fake_common = _types.ModuleType("common_lib")
    fake_common.is_market_open = lambda: True
    monkeypatch.setitem(sys.modules, "common_lib", fake_common)

    fake_notif = _types.ModuleType("notifications.service")
    fake_notif.dispatch = _fake_dispatch
    monkeypatch.setitem(sys.modules, "notifications.service", fake_notif)

    fake_kite = _make_fake_kite(
        positions_net=[_pos("NIFTY24DEC18000PE", -50)],
        gtts=[_gtt("NIFTY24DEC18000PE", 100)],
    )
    monkeypatch.setattr(gtt_monitor, "_build_kite_client", lambda: fake_kite)

    gtt_monitor.run_gtt_monitor_check()
    gtt_monitor.run_gtt_monitor_check()

    assert len(dispatched) == 1


def test_dedup_re_alerts_after_mismatch_clears(monkeypatch):
    """Mismatch clears then reappears → notification fires again."""
    _reset_monitor_state()

    dispatched: list[str] = []

    def _fake_dispatch(notif_type, title="", body="", metadata=None):
        dispatched.append(notif_type)
        return 1

    import types as _types

    fake_common = _types.ModuleType("common_lib")
    fake_common.is_market_open = lambda: True
    monkeypatch.setitem(sys.modules, "common_lib", fake_common)

    fake_notif = _types.ModuleType("notifications.service")
    fake_notif.dispatch = _fake_dispatch
    monkeypatch.setitem(sys.modules, "notifications.service", fake_notif)

    call_count = {"n": 0}

    def _kite_factory():
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Second call: position closed, GTT gone
            return _make_fake_kite(positions_net=[], gtts=[])
        return _make_fake_kite(
            positions_net=[_pos("NIFTY24DEC18000PE", -50)],
            gtts=[_gtt("NIFTY24DEC18000PE", 100)],
        )

    monkeypatch.setattr(gtt_monitor, "_build_kite_client", _kite_factory)

    gtt_monitor.run_gtt_monitor_check()   # mismatch found → alert fires
    gtt_monitor.run_gtt_monitor_check()   # mismatch gone → no alert
    gtt_monitor.run_gtt_monitor_check()   # mismatch reappears → alert fires again

    assert len(dispatched) == 2
