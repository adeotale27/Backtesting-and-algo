"""Unit tests for CAS Expiry strike math, detector logic, and backtest."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, time
from unittest.mock import MagicMock

import pytest

from cas_expiry.backtest import bs_price, intrinsic, run_cas_backtest
from cas_expiry.cas_detector import CasCloseDetector, CloseSignal
from cas_expiry.config import load_config
from cas_expiry.state import StateStore, FillRecord
from cas_expiry.strikes import round_atm, target_strikes as strike_targets
from cas_expiry.time_utils import in_window
from datetime import datetime, timezone, timedelta


IST = timezone(timedelta(hours=5, minutes=30))


def test_round_atm_nifty():
    assert round_atm(24837, 50) == 24850
    assert round_atm(24824, 50) == 24800
    assert round_atm(24825, 50) == 24800 or round_atm(24825, 50) == 24850  # banker's/round


def test_target_strikes_atm1():
    atm, ce, pe = strike_targets(24850, 50, 1, 1)
    assert atm == 24850
    assert ce == 24900
    assert pe == 24800


def test_target_strikes_sensex():
    atm, ce, pe = strike_targets(81123, 100, 1, 1)
    assert atm == 81100
    assert ce == 81200
    assert pe == 81000


def test_in_window():
    now = datetime(2026, 8, 6, 15, 29, 0, tzinfo=IST)
    assert in_window(now, time(15, 28), time(15, 35))
    now2 = datetime(2026, 8, 6, 15, 27, 0, tzinfo=IST)
    assert not in_window(now2, time(15, 28), time(15, 35))


def test_bs_and_intrinsic():
    # Deep ITM call intrinsic ~ 100
    assert intrinsic(25000, 24900, "CE") == 100
    assert intrinsic(25000, 24900, "PE") == 0
    prem = bs_price(24850, 24900, 18.0, 5.0 / (60 * 24), "CE")
    assert prem >= 0
    # Near ATM with tiny T should be small
    assert prem < 200


def test_detector_ohlc_change():
    kite = MagicMock()
    # First quote: baseline
    kite.quote.side_effect = [
        {
            "NSE:NIFTY 50": {
                "last_price": 24840,
                "ohlc": {"open": 24700, "high": 24900, "low": 24650, "close": 24780},
            }
        },
        {
            "NSE:NIFTY 50": {
                "last_price": 24855,
                "ohlc": {"open": 24700, "high": 24900, "low": 24650, "close": 24850},
            }
        },
    ]
    det = CasCloseDetector(
        kite, "NIFTY", time(15, 28), time(15, 35), poll_interval_seconds=0.01
    )
    base = det.capture_baseline()
    assert base == 24780
    signal = det.check_once()
    assert signal is not None
    assert isinstance(signal, CloseSignal)
    assert signal.close_price == 24850
    assert signal.source == "ohlc_close"


def test_detector_no_change():
    kite = MagicMock()
    q = {
        "NSE:NIFTY 50": {
            "last_price": 24840,
            "ohlc": {"close": 24780},
        }
    }
    kite.quote.return_value = q
    det = CasCloseDetector(kite, "NIFTY", time(15, 28), time(15, 35))
    det.capture_baseline()
    assert det.check_once() is None


def test_state_activate_fire(tmp_path):
    path = str(tmp_path / "state.json")
    store = StateStore(path)
    store.activate("tester")
    assert store.is_activated()
    fills = [
        FillRecord(
            ts="2026-08-06T15:30:01+05:30",
            index="NIFTY",
            leg="CE",
            tradingsymbol="NIFTY2680624900CE",
            strike=24900,
            side="SELL",
            quantity=65,
            order_id=-1,
            price=12.5,
            dry_run=True,
        )
    ]
    store.mark_fired(24850, fills)
    assert store.has_fired_today()
    snap = store.snapshot()
    assert snap["last_close_price"] == 24850
    assert len(snap["fills"]) == 1


def test_backtest_synthetic():
    result = run_cas_backtest(
        kite=None,
        index="NIFTY",
        start=date(2026, 5, 1),
        end=date(2026, 7, 31),
        capital=500_000,
        lots=1,
        assumed_iv=18.0,
    )
    assert result.num_trades > 0
    assert result.initial_capital == 500_000
    d = result.to_dict()
    assert "equity_curve" in d
    assert "trades" in d
    # Short OTM-ish ATM±1 on expiry usually small positive theta capture in model
    assert isinstance(result.total_pnl, float)


def test_load_config_from_example(tmp_path):
    example = os.path.join(
        os.path.dirname(__file__), "..", "config.ini.example"
    )
    # copy example into temp as config
    import shutil

    dest = tmp_path / "config.ini"
    shutil.copy(example, dest)
    cfg = load_config(str(dest))
    assert cfg.index == "NIFTY"
    assert cfg.live_trading is False
    assert cfg.poll_interval_ms == 50
    assert cfg.indexes() == ["NIFTY"]
