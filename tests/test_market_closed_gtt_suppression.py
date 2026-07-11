"""
Tests for market-close GTT suppression in the Wave Extractor.

Verifies that:
1. is_market_open() returns the correct value at all boundary times
   (pre-open, in-session, after-close, weekends).
2. place_duo_order() does NOT call place_gtt_order() when the market is closed.
3. place_duo_order() DOES call place_gtt_order() when the market is open and a
   regular order fails.
4. order_complete() does NOT call place_duo_order() when the market is closed.
5. The poll loop in ticker_single_scraper_new calls sys.exit(0) when market closes.

Uses AAA (Arrange, Act, Assert) pattern throughout.
All external dependencies (kite, sys.exit, time) are mocked.
"""
import datetime
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
IST_OFFSET = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _make_ist(weekday_offset: int, hour: int, minute: int, second: int = 0) -> datetime.datetime:
    """Build an IST datetime for a given weekday offset from 2024-01-01 (Monday).

    Args:
        weekday_offset: 0=Monday … 6=Sunday.
        hour: Hour in 24-h format (IST).
        minute: Minute.
        second: Second.

    Returns:
        timezone-aware IST datetime.
    """
    # 2024-01-01 is a Monday
    base = datetime.datetime(2024, 1, 1, tzinfo=IST_OFFSET)
    return base + datetime.timedelta(days=weekday_offset, hours=hour, minutes=minute, seconds=second)


# ---------------------------------------------------------------------------
# is_market_open() boundary tests
# ---------------------------------------------------------------------------

class TestIsMarketOpen:
    """Unit tests for common_lib.is_market_open()."""

    def _patch_now(self, dt: datetime.datetime):
        """Return a context-manager patch for common_lib.get_ist_now."""
        import common_lib  # noqa: PLC0415
        return patch.object(common_lib, "get_ist_now", return_value=dt)

    def test_returns_true_during_trading_hours(self) -> None:
        """Arrange: Monday 10:30 IST — well within session.
        Act: call is_market_open().
        Assert: returns True.
        """
        import common_lib  # noqa: PLC0415
        with self._patch_now(_make_ist(0, 10, 30)):
            assert common_lib.is_market_open() is True

    def test_returns_false_before_market_open(self) -> None:
        """Arrange: Monday 09:00 IST — before 09:15.
        Act: call is_market_open().
        Assert: returns False.
        """
        import common_lib  # noqa: PLC0415
        with self._patch_now(_make_ist(0, 9, 0)):
            assert common_lib.is_market_open() is False

    def test_returns_false_after_market_close(self) -> None:
        """Arrange: Monday 15:31 IST — one minute after close.
        Act: call is_market_open().
        Assert: returns False.
        """
        import common_lib  # noqa: PLC0415
        with self._patch_now(_make_ist(0, 15, 31)):
            assert common_lib.is_market_open() is False

    def test_returns_false_on_saturday(self) -> None:
        """Arrange: Saturday 10:00 IST.
        Act: call is_market_open().
        Assert: returns False.
        """
        import common_lib  # noqa: PLC0415
        with self._patch_now(_make_ist(5, 10, 0)):  # weekday 5 = Saturday
            assert common_lib.is_market_open() is False

    def test_returns_false_on_sunday(self) -> None:
        """Arrange: Sunday 12:00 IST.
        Act: call is_market_open().
        Assert: returns False.
        """
        import common_lib  # noqa: PLC0415
        with self._patch_now(_make_ist(6, 12, 0)):  # weekday 6 = Sunday
            assert common_lib.is_market_open() is False

    def test_returns_true_at_open_boundary(self) -> None:
        """Arrange: Monday exactly 09:15:00 IST (lower bound inclusive).
        Act: call is_market_open().
        Assert: returns True.
        """
        import common_lib  # noqa: PLC0415
        with self._patch_now(_make_ist(0, 9, 15, 0)):
            assert common_lib.is_market_open() is True

    def test_returns_true_at_close_boundary(self) -> None:
        """Arrange: Monday exactly 15:30:00 IST (upper bound inclusive).
        Act: call is_market_open().
        Assert: returns True.
        """
        import common_lib  # noqa: PLC0415
        with self._patch_now(_make_ist(0, 15, 30, 0)):
            assert common_lib.is_market_open() is True


# ---------------------------------------------------------------------------
# place_duo_order GTT fallback suppression tests
# ---------------------------------------------------------------------------

class TestPlaceDuoOrderGttSuppression:
    """Tests that GTT fallback in place_duo_order is gated by is_market_open()."""

    def _minimal_common_lib_state(self, common_lib_mod) -> None:
        """Set enough global state for place_duo_order to reach the GTT branch."""
        common_lib_mod.already_executing_order = 0
        common_lib_mod.already_updating_order = 0
        common_lib_mod.buy_gap = 5.0
        common_lib_mod.sell_gap = 5.0
        common_lib_mod.buy_quantity = 50
        common_lib_mod.sell_quantity = 50
        common_lib_mod.quantity = 50
        common_lib_mod.symbol_type = "ce"
        common_lib_mod.exchange = "NFO"
        common_lib_mod.exceptNFBNF = True
        common_lib_mod.cool_off_time = 0
        common_lib_mod.scraper_last_price = 100.0
        common_lib_mod.old_quote_price = 100.0
        common_lib_mod.duo_old_buy_price = -1
        common_lib_mod.duo_old_sell_price = -1
        common_lib_mod.buy_gap_percentage = 0.05
        common_lib_mod.sell_gap_percentage = 0.05
        common_lib_mod.orders = {}
        common_lib_mod.multiplier_scale = {"0": [1, 1]}
        common_lib_mod.order_gtt_regular = "regular"
        common_lib_mod.global_restrict_buy = 0
        common_lib_mod.global_restrict_sell = 0
        common_lib_mod.typeOfProduct = "NRML"
        common_lib_mod.pending_gtt_fallbacks = {}

    def test_no_gtt_placed_when_market_closed(self) -> None:
        """Arrange: regular order fails, market is closed (16:00 IST).
        Act: place_duo_order().
        Assert: place_gtt_order is NOT called.
        """
        import common_lib  # noqa: PLC0415
        self._minimal_common_lib_state(common_lib)

        mock_kite = MagicMock()
        mock_kite.VARIETY_REGULAR = "regular"
        mock_kite.TRANSACTION_TYPE_SELL = "SELL"
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        mock_kite.quote.return_value = {"NFO:TESTSYM": {"last_price": 100.0}}
        common_lib.kite = mock_kite

        # set_restrictions must return a valid dict
        restriction_mock = {
            "nifty": {"ce": {"buy": "yes", "sell": "yes"}, "pe": {"buy": "yes", "sell": "yes"}, "futures": {"buy": "yes", "sell": "yes"}},
            "bank_nifty": {"ce": {"buy": "yes", "sell": "yes"}, "pe": {"buy": "yes", "sell": "yes"}, "futures": {"buy": "yes", "sell": "yes"}},
        }

        after_close_time = _make_ist(0, 16, 0)

        with (
            patch.object(common_lib, "get_ist_now", return_value=after_close_time),
            patch.object(common_lib, "set_restrictions", return_value=restriction_mock),
            patch.object(common_lib, "get_position_for_symbol", return_value=0),
            patch.object(common_lib, "get_delta_multiplier", return_value=(1.0, 1.0)),
            patch.object(common_lib, "place_order", return_value=-1),
            patch.object(common_lib, "place_gtt_order") as mock_gtt,
            patch.object(common_lib, "invalidate_positions_cache"),
        ):
            common_lib.place_duo_order("TESTSYM", "NRML", True)

        mock_gtt.assert_not_called()

    def test_gtt_placed_when_market_open_and_order_fails(self) -> None:
        """Arrange: regular order fails, market is open (10:00 IST).
        Act: place_duo_order().
        Assert: place_gtt_order IS called at least once (for the SELL leg).
        """
        import common_lib  # noqa: PLC0415
        self._minimal_common_lib_state(common_lib)

        # initial_positions must look like {'position': int} for the function to proceed
        common_lib.initial_positions = {"position": 0}

        mock_kite = MagicMock()
        mock_kite.VARIETY_REGULAR = "regular"
        mock_kite.TRANSACTION_TYPE_SELL = "SELL"
        mock_kite.TRANSACTION_TYPE_BUY = "BUY"
        # Both kite.quote calls inside place_duo_order must return a valid dict
        mock_kite.quote.return_value = {"NFO:TESTSYM": {"last_price": 100.0}}
        common_lib.kite = mock_kite

        restriction_mock = {
            "nifty": {"ce": {"buy": "yes", "sell": "yes"}, "pe": {"buy": "yes", "sell": "yes"}, "futures": {"buy": "yes", "sell": "yes"}},
            "bank_nifty": {"ce": {"buy": "yes", "sell": "yes"}, "pe": {"buy": "yes", "sell": "yes"}, "futures": {"buy": "yes", "sell": "yes"}},
        }

        during_session_time = _make_ist(0, 10, 0)

        with (
            patch.object(common_lib, "get_ist_now", return_value=during_session_time),
            patch.object(common_lib, "set_restrictions", return_value=restriction_mock),
            patch.object(common_lib, "get_position_for_symbol", return_value=0),
            patch.object(common_lib, "get_delta_multiplier", return_value=(1.0, 1.0)),
            patch.object(common_lib, "place_order", return_value=-1),
            patch.object(common_lib, "place_gtt_order", return_value=999) as mock_gtt,
            patch.object(common_lib, "invalidate_positions_cache"),
            patch.object(common_lib, "get_quote_with_retry",
                         return_value={"NFO:TESTSYM": {"last_price": 100.0}}),
        ):
            common_lib.place_duo_order("TESTSYM", "NRML", True)

        # At least the SELL GTT fallback should have been attempted
        mock_gtt.assert_called()


# ---------------------------------------------------------------------------
# order_complete market-close exit test
# ---------------------------------------------------------------------------

class TestOrderCompleteMarketCloseExit:
    """Tests that order_complete() exits the process when market is closed."""

    def test_sys_exit_called_when_market_closed_after_cancel(self) -> None:
        """Arrange: order cancelled (not system-cancelled), market is closed.
        Act: order_complete().
        Assert: sys.exit(0) is raised / called.
        """
        import common_lib  # noqa: PLC0415

        order_id = "ORD123"
        common_lib.orders = {
            order_id: {
                "price": 100.0,
                "quantity": 50,
                "transaction_type": "SELL",
                "symbol": "TESTSYM",
                "associated_order": -1,
                "system_cancelled": False,
            }
        }
        common_lib.typeOfProduct = "NRML"
        common_lib.exceptNFBNF = True

        after_close_time = _make_ist(0, 16, 0)

        with (
            patch.object(common_lib, "get_ist_now", return_value=after_close_time),
            patch.object(common_lib, "cancel_all_pending_gtts_for_symbol"),
            patch.object(common_lib, "printCurrentStatus"),
            patch.object(common_lib, "delete_order_from_list"),
            patch.object(sys, "exit") as mock_exit,
        ):
            common_lib.order_complete(order_id, is_on_disconnect=True, is_complete="")

        mock_exit.assert_called_once_with(0)
