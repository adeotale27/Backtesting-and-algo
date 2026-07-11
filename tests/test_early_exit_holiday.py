"""
Tests for holiday-aware trading-day lookback in early_exit_lib.

Covers _get_prev_trading_day_with_candle() and verifies that build_preview
falls back to the last real trading day when the immediately preceding weekday
has no candle data (public holiday scenario).
"""

from __future__ import annotations

import os
import sys
from datetime import date
from typing import Dict, Optional
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors the pattern used by all other tests in this directory
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Helpers under test (imported after path is fixed)
# ---------------------------------------------------------------------------
from early_exit_lib import (  # noqa: E402
    _get_prev_trading_day,
    _get_prev_trading_day_with_candle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_kite(candle_map: Dict[date, Optional[float]]) -> MagicMock:
    """Return a mock kite whose historical_data returns data keyed by date.

    candle_map: {date_obj: close_price_or_None}
    If the date is absent or the value is None, historical_data returns [].
    """
    kite = MagicMock()

    def _hist(token, from_dt, to_dt, interval):
        d = from_dt.date() if hasattr(from_dt, "date") else from_dt
        price = candle_map.get(d)
        if price is None:
            return []
        return [{"close": price, "open": price, "high": price, "low": price, "volume": 0}]

    kite.historical_data.side_effect = _hist
    return kite


# ---------------------------------------------------------------------------
# _get_prev_trading_day_with_candle
# ---------------------------------------------------------------------------


class TestGetPrevTradingDayWithCandle:
    """Unit tests for the holiday-aware lookback helper."""

    def test_normal_day_returns_previous_weekday(self) -> None:
        """Arrange: candle available on the immediately previous weekday."""
        # 2026-04-15 is a Wednesday; prev weekday is Tuesday 2026-04-14
        candle_map = {date(2026, 4, 14): 80000.0}
        kite = _make_kite(candle_map)

        trading_day, close = _get_prev_trading_day_with_candle(
            kite, 12345, date(2026, 4, 15), 15, 27
        )

        assert trading_day == date(2026, 4, 14)
        assert close == pytest.approx(80000.0)

    def test_skips_holiday_and_returns_day_before(self) -> None:
        """Arrange: yesterday (Tue 2026-04-14) is a holiday; expect Mon 2026-04-13.

        Calendar context:
            2026-04-15 = Wed (today/from_date)
            2026-04-14 = Tue (BSE holiday)
            2026-04-13 = Mon (last real trading day)
        """
        candle_map = {
            date(2026, 4, 14): None,    # holiday — no candle
            date(2026, 4, 13): 79500.0, # last real trading day
        }
        kite = _make_kite(candle_map)

        trading_day, close = _get_prev_trading_day_with_candle(
            kite, 12345, date(2026, 4, 15), 15, 27
        )

        assert trading_day == date(2026, 4, 13)
        assert close == pytest.approx(79500.0)

    def test_skips_weekend_automatically(self) -> None:
        """Arrange: querying from Monday 2026-04-13 should jump over Sun+Sat to Fri 2026-04-10.

        Calendar context:
            2026-04-13 = Mon (from_date, exclusive)
            2026-04-12 = Sun (skipped)
            2026-04-11 = Sat (skipped)
            2026-04-10 = Fri (first weekday candidate, has data)
        """
        candle_map = {date(2026, 4, 10): 78000.0}  # Friday
        kite = _make_kite(candle_map)

        trading_day, close = _get_prev_trading_day_with_candle(
            kite, 12345, date(2026, 4, 13), 15, 27  # from Monday
        )

        assert trading_day == date(2026, 4, 10)  # Friday
        assert close == pytest.approx(78000.0)

    def test_skips_multiple_consecutive_holidays(self) -> None:
        """Arrange: two consecutive weekday holidays before a real trading day.

        Calendar context:
            2026-04-15 = Wed (from_date)
            2026-04-14 = Tue (holiday)
            2026-04-13 = Mon (holiday)
            2026-04-12 = Sun (weekend, not queried)
            2026-04-11 = Sat (weekend, not queried)
            2026-04-10 = Fri (real trading day with data)
        """
        candle_map = {
            date(2026, 4, 14): None,    # Tue holiday
            date(2026, 4, 13): None,    # Mon holiday
            date(2026, 4, 10): 77000.0, # Fri has data
        }
        kite = _make_kite(candle_map)

        trading_day, close = _get_prev_trading_day_with_candle(
            kite, 12345, date(2026, 4, 15), 15, 27
        )

        assert trading_day == date(2026, 4, 10)
        assert close == pytest.approx(77000.0)

    def test_returns_none_when_max_lookback_exhausted(self) -> None:
        """Arrange: no data in any of the max_lookback days."""
        kite = _make_kite({})   # no data anywhere

        trading_day, close = _get_prev_trading_day_with_candle(
            kite, 12345, date(2026, 4, 15), 15, 27, max_lookback=3
        )

        assert trading_day is None
        assert close is None

    def test_does_not_call_api_for_weekends(self) -> None:
        """Verify historical_data is never called for Sat or Sun."""
        candle_map = {date(2026, 4, 10): 75000.0}  # Fri
        kite = _make_kite(candle_map)

        _get_prev_trading_day_with_candle(
            kite, 12345, date(2026, 4, 13), 15, 27  # Monday
        )

        called_dates = [
            c.args[1].date() if hasattr(c.args[1], "date") else c.args[1]
            for c in kite.historical_data.call_args_list
        ]
        for d in called_dates:
            assert d.weekday() < 5, f"historical_data called for weekend date {d}"

    def test_zero_close_treated_as_no_data(self) -> None:
        """A candle with close=0 should be skipped in favour of an earlier date.

        Calendar context:
            2026-04-15 = Wed (from_date)
            2026-04-14 = Tue (bad candle, close=0)
            2026-04-13 = Mon (real data)
        """
        candle_map = {
            date(2026, 4, 14): 0.0,    # stale / bad candle
            date(2026, 4, 13): 81000.0,
        }
        kite = _make_kite(candle_map)

        trading_day, close = _get_prev_trading_day_with_candle(
            kite, 12345, date(2026, 4, 15), 15, 27
        )

        assert trading_day == date(2026, 4, 13)
        assert close == pytest.approx(81000.0)


# ---------------------------------------------------------------------------
# Integration-style: _get_prev_trading_day unchanged behaviour
# ---------------------------------------------------------------------------


class TestGetPrevTradingDayBasic:
    """Regression tests — original weekday-skipping behaviour must remain.

    Calendar context for 2026-04:
        Sat Apr 11, Sun Apr 12, Mon Apr 13, Tue Apr 14, Wed Apr 15
        Fri Apr 10
    """

    def test_skips_saturday(self) -> None:
        """Sunday (Apr 12) -> Friday (Apr 10)."""
        assert _get_prev_trading_day(date(2026, 4, 12)) == date(2026, 4, 10)

    def test_skips_sunday(self) -> None:
        """Saturday (Apr 11) -> Friday (Apr 10)."""
        assert _get_prev_trading_day(date(2026, 4, 11)) == date(2026, 4, 10)

    def test_monday_goes_to_friday(self) -> None:
        """Monday (Apr 13) -> Friday (Apr 10)."""
        assert _get_prev_trading_day(date(2026, 4, 13)) == date(2026, 4, 10)

    def test_tuesday_goes_to_monday(self) -> None:
        """Tuesday (Apr 14) -> Monday (Apr 13)."""
        assert _get_prev_trading_day(date(2026, 4, 14)) == date(2026, 4, 13)

    def test_wednesday_goes_to_tuesday(self) -> None:
        """Wednesday (Apr 15) -> Tuesday (Apr 14)."""
        assert _get_prev_trading_day(date(2026, 4, 15)) == date(2026, 4, 14)
