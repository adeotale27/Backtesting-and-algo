"""
Unit tests for trade_journal.py.

Tests cover:
- classify_source: algo_source → name/category mapping
- pair_trades: FIFO matching for sell-first, buy-first, partial fills, unpaired
- attribute_pnl: All 6 attribution rule combinations
- load_orders_range: Multi-day file loading
- get_journal_summary: Aggregate calculations
- save_trade_note / get_trade_notes: Notes persistence
"""

import pytest
import json
import os
import tempfile
from datetime import date
from unittest.mock import patch, MagicMock


# ============================================================================
# Test classify_source
# ============================================================================

class TestClassifySource:
    """Tests for classify_source function."""

    def test_survivor_nifty(self):
        """Test Survivor NIFTY classification."""
        from trade_journal import classify_source

        # Arrange & Act
        result = classify_source("Trending_Market_Code")

        # Assert
        assert result["algo_name"] == "Survivor (NIFTY)"
        assert result["category"] == "Breakout"
        assert result["is_algo"] is True

    def test_survivor_sensex(self):
        """Test Survivor SENSEX classification."""
        from trade_journal import classify_source

        result = classify_source("Trend_Mkt_SENSEX")

        assert result["algo_name"] == "Survivor (SENSEX)"
        assert result["category"] == "Breakout"
        assert result["is_algo"] is True

    def test_survivor_stock(self):
        """Test Survivor Stock classification."""
        from trade_journal import classify_source

        result = classify_source("Trend_Mkt_Stock")

        assert result["algo_name"] == "Survivor (Stock)"
        assert result["category"] == "Breakout"
        assert result["is_algo"] is True

    def test_scraper(self):
        """Test Wave Extractor Scraper classification."""
        from trade_journal import classify_source

        result = classify_source("Scraper")

        assert result["algo_name"] == "Wave Extractor (Scraper)"
        assert result["category"] == "Mean Reversion"
        assert result["is_algo"] is True

    def test_gap_odr_manual(self):
        """Test Gap-Odr_Manual is classified as Manual/Bulk Order."""
        from trade_journal import classify_source

        result = classify_source("Gap-Odr_Manual")

        assert result["algo_name"] == "Manual"
        assert result["category"] == "Bulk Order"
        assert result["is_algo"] is False

    def test_gap_odr_auto(self):
        """Test Gap-Odr_Auto is classified as Manual/Bulk Order."""
        from trade_journal import classify_source

        result = classify_source("Gap-Odr_Auto")

        assert result["algo_name"] == "Manual"
        assert result["category"] == "Bulk Order"
        assert result["is_algo"] is False

    def test_unknown_tag(self):
        """Test Unknown is classified as Unknown/Bulk Order."""
        from trade_journal import classify_source

        result = classify_source("Unknown")

        assert result["algo_name"] == "Unknown"
        assert result["category"] == "Bulk Order"
        assert result["is_algo"] is False

    def test_sl_order(self):
        """Test SL_Order is classified as Stop Loss/Manual."""
        from trade_journal import classify_source

        result = classify_source("SL_Order")

        assert result["algo_name"] == "Stop Loss"
        assert result["category"] == "Manual"
        assert result["is_algo"] is False

    def test_unknown_source_fallback(self):
        """Test unknown algo_source returns fallback."""
        from trade_journal import classify_source

        result = classify_source("SomeNewAlgo")

        assert result["algo_name"] == "SomeNewAlgo"
        assert result["category"] == "Other"
        assert result["is_algo"] is False


# ============================================================================
# Test pair_trades
# ============================================================================

class TestPairTrades:
    """Tests for pair_trades FIFO matching."""

    def test_sell_first_basic(self):
        """Test basic sell-first pairing."""
        from trade_journal import pair_trades

        # Arrange
        orders = [
            {"symbol": "NIFTY24JAN23000PE", "transaction_type": "SELL",
             "price": 100, "quantity": 75, "timestamp": "2026-02-12T10:00:00",
             "algo_source": "Trending_Market_Code", "option_type": "PE"},
            {"symbol": "NIFTY24JAN23000PE", "transaction_type": "BUY",
             "price": 80, "quantity": 75, "timestamp": "2026-02-12T11:00:00",
             "algo_source": "Unknown", "option_type": "PE"},
        ]

        # Act
        round_trips, unpaired = pair_trades(orders)

        # Assert
        assert len(round_trips) == 1
        assert len(unpaired) == 0
        assert round_trips[0]["direction"] == "sell_first"
        # P&L = (100 - 80) * 75 = 1500
        assert round_trips[0]["pnl"] == 1500.0
        assert round_trips[0]["matched_qty"] == 75

    def test_buy_first_basic(self):
        """Test basic buy-first pairing."""
        from trade_journal import pair_trades

        orders = [
            {"symbol": "NIFTY24JAN23000CE", "transaction_type": "BUY",
             "price": 50, "quantity": 75, "timestamp": "2026-02-12T10:00:00",
             "algo_source": "Trending_Market_Code", "option_type": "CE"},
            {"symbol": "NIFTY24JAN23000CE", "transaction_type": "SELL",
             "price": 80, "quantity": 75, "timestamp": "2026-02-12T11:00:00",
             "algo_source": "Unknown", "option_type": "CE"},
        ]

        round_trips, unpaired = pair_trades(orders)

        assert len(round_trips) == 1
        assert round_trips[0]["direction"] == "buy_first"
        # P&L = (80 - 50) * 75 = 2250
        assert round_trips[0]["pnl"] == 2250.0

    def test_unpaired_sell_only(self):
        """Test sell with no matching buy remains unpaired."""
        from trade_journal import pair_trades

        orders = [
            {"symbol": "NIFTY24JAN23000PE", "transaction_type": "SELL",
             "price": 100, "quantity": 75, "timestamp": "2026-02-12T10:00:00",
             "algo_source": "Unknown", "option_type": "PE"},
        ]

        round_trips, unpaired = pair_trades(orders)

        assert len(round_trips) == 0
        assert len(unpaired) == 1

    def test_partial_quantity_match(self):
        """Test partial quantity matching leaves remainder unpaired."""
        from trade_journal import pair_trades

        orders = [
            {"symbol": "NIFTY24JAN23000PE", "transaction_type": "SELL",
             "price": 100, "quantity": 150, "timestamp": "2026-02-12T10:00:00",
             "algo_source": "Unknown", "option_type": "PE"},
            {"symbol": "NIFTY24JAN23000PE", "transaction_type": "BUY",
             "price": 80, "quantity": 75, "timestamp": "2026-02-12T11:00:00",
             "algo_source": "Unknown", "option_type": "PE"},
        ]

        round_trips, unpaired = pair_trades(orders)

        assert len(round_trips) == 1
        assert round_trips[0]["matched_qty"] == 75
        # P&L = (100 - 80) * 75 = 1500
        assert round_trips[0]["pnl"] == 1500.0
        # Remaining 75 sell quantity unpaired
        assert len(unpaired) == 1
        assert unpaired[0]["quantity"] == 75

    def test_multiple_rounds_same_symbol(self):
        """Test multiple round trips for the same symbol (FIFO)."""
        from trade_journal import pair_trades

        orders = [
            {"symbol": "SYM", "transaction_type": "SELL",
             "price": 100, "quantity": 50, "timestamp": "T1",
             "algo_source": "Unknown", "option_type": "PE"},
            {"symbol": "SYM", "transaction_type": "SELL",
             "price": 120, "quantity": 50, "timestamp": "T2",
             "algo_source": "Scraper", "option_type": "PE"},
            {"symbol": "SYM", "transaction_type": "BUY",
             "price": 80, "quantity": 50, "timestamp": "T3",
             "algo_source": "Unknown", "option_type": "PE"},
            {"symbol": "SYM", "transaction_type": "BUY",
             "price": 90, "quantity": 50, "timestamp": "T4",
             "algo_source": "Unknown", "option_type": "PE"},
        ]

        round_trips, unpaired = pair_trades(orders)

        assert len(round_trips) == 2
        assert len(unpaired) == 0
        # First sell (100) matched with first buy (80) => P&L = 20*50 = 1000
        assert round_trips[0]["pnl"] == 1000.0
        # Second sell (120) matched with second buy (90) => P&L = 30*50 = 1500
        assert round_trips[1]["pnl"] == 1500.0

    def test_empty_orders(self):
        """Test empty order list returns no trips."""
        from trade_journal import pair_trades

        round_trips, unpaired = pair_trades([])

        assert len(round_trips) == 0
        assert len(unpaired) == 0


# ============================================================================
# Test attribute_pnl
# ============================================================================

class TestAttributePnl:
    """Tests for all 6 P&L attribution rule combinations."""

    def _make_round_trip(self, direction, open_source, close_source, pnl):
        """Helper to create a round trip dict."""
        return {
            "symbol": "TEST",
            "direction": direction,
            "pnl": pnl,
            "open_trade": {"algo_source": open_source, "timestamp": "T1",
                           "price": 100, "quantity": 75, "trade_date": "2026-01-01",
                           "transaction_type": "SELL" if direction == "sell_first" else "BUY"},
            "close_trade": {"algo_source": close_source, "timestamp": "T2",
                            "price": 80, "quantity": 75, "trade_date": "2026-01-01",
                            "transaction_type": "BUY" if direction == "sell_first" else "SELL"},
            "matched_qty": 75,
            "option_type": "PE",
            "expiry": "24JAN",
        }

    def test_sell_first_algo_open_manual_close(self):
        """Sell-first: Algo sell → 100% to algo."""
        from trade_journal import attribute_pnl

        rt = self._make_round_trip("sell_first", "Trending_Market_Code", "Unknown", 1500)
        result = attribute_pnl(rt)

        assert result["algo_pnl"] == 1500
        assert result["manual_pnl"] == 0
        assert result["attribution_label"] == "Survivor (NIFTY)"

    def test_sell_first_algo_open_algo_close(self):
        """Sell-first: Algo sell + Algo buy → 100% to opening algo."""
        from trade_journal import attribute_pnl

        rt = self._make_round_trip("sell_first", "Scraper", "Trending_Market_Code", 1000)
        result = attribute_pnl(rt)

        assert result["algo_pnl"] == 1000
        assert result["manual_pnl"] == 0
        assert result["attribution_label"] == "Wave Extractor (Scraper)"

    def test_sell_first_manual_open_manual_close(self):
        """Sell-first: Manual sell + Manual buy → 100% Manual."""
        from trade_journal import attribute_pnl

        rt = self._make_round_trip("sell_first", "Unknown", "Gap-Odr_Manual", 500)
        result = attribute_pnl(rt)

        assert result["algo_pnl"] == 0
        assert result["manual_pnl"] == 500
        assert result["attribution_label"] == "Manual"

    def test_sell_first_manual_open_algo_close(self):
        """Sell-first: Manual sell + Algo buy → 100% to closing algo."""
        from trade_journal import attribute_pnl

        rt = self._make_round_trip("sell_first", "Unknown", "Scraper", 1000)
        result = attribute_pnl(rt)

        assert result["algo_pnl"] == 1000
        assert result["manual_pnl"] == 0
        assert result["attribution_label"] == "Wave Extractor (Scraper)"

    def test_buy_first_algo_open_manual_close(self):
        """Buy-first: Algo buy + Manual close → 100% to algo."""
        from trade_journal import attribute_pnl

        rt = self._make_round_trip("buy_first", "Trending_Market_Code", "Unknown", 2000)
        result = attribute_pnl(rt)

        assert result["algo_pnl"] == 2000
        assert result["manual_pnl"] == 0
        assert result["attribution_label"] == "Survivor (NIFTY)"

    def test_buy_first_manual_open_manual_close(self):
        """Buy-first: Manual buy + Manual close → 100% Manual."""
        from trade_journal import attribute_pnl

        rt = self._make_round_trip("buy_first", "Unknown", "Unknown", -500)
        result = attribute_pnl(rt)

        assert result["algo_pnl"] == 0
        assert result["manual_pnl"] == -500
        assert result["attribution_label"] == "Manual"

    def test_buy_first_manual_open_algo_close(self):
        """Buy-first: Manual buy + Algo close → 50% Algo / 50% Manual."""
        from trade_journal import attribute_pnl

        rt = self._make_round_trip("buy_first", "Gap-Odr_Auto", "Scraper", 300)
        result = attribute_pnl(rt)

        assert result["algo_pnl"] == 150
        assert result["manual_pnl"] == 150
        assert "50%" in result["attribution_label"]


# ============================================================================
# Test load_orders_range
# ============================================================================

class TestLoadOrdersRange:
    """Tests for multi-day order loading."""

    @patch('trade_journal._get_status_dir')
    def test_loads_multiple_days(self, mock_status_dir):
        """Test loading orders from two consecutive days."""
        from trade_journal import load_orders_range

        temp_dir = tempfile.mkdtemp()
        mock_status_dir.return_value = temp_dir

        # Create two day files
        for day, orders in [
            ("2026-02-12", [{"timestamp": "2026-02-12T10:00:00", "symbol": "A"}]),
            ("2026-02-13", [{"timestamp": "2026-02-13T09:00:00", "symbol": "B"}]),
        ]:
            filepath = os.path.join(temp_dir, f"executed_orders_{day}.json")
            with open(filepath, "w") as f:
                json.dump({"orders": orders}, f)

        # Act
        result = load_orders_range(date(2026, 2, 12), date(2026, 2, 13))

        # Assert
        assert len(result) == 2
        assert result[0]["symbol"] == "A"  # Sorted by timestamp ascending
        assert result[1]["symbol"] == "B"

        # Cleanup
        import shutil
        shutil.rmtree(temp_dir)

    @patch('trade_journal._get_status_dir')
    def test_handles_missing_days(self, mock_status_dir):
        """Test skips dates with no order files."""
        from trade_journal import load_orders_range

        temp_dir = tempfile.mkdtemp()
        mock_status_dir.return_value = temp_dir

        # Only create one file
        filepath = os.path.join(temp_dir, "executed_orders_2026-02-12.json")
        with open(filepath, "w") as f:
            json.dump({"orders": [{"timestamp": "T1", "symbol": "A"}]}, f)

        # Act - range covers 3 days but only 1 has data
        result = load_orders_range(date(2026, 2, 11), date(2026, 2, 13))

        # Assert
        assert len(result) == 1

        import shutil
        shutil.rmtree(temp_dir)


# ============================================================================
# Test get_journal_summary
# ============================================================================

class TestGetJournalSummary:
    """Tests for aggregate summary calculations."""

    @patch('trade_journal.load_orders_range')
    def test_basic_summary(self, mock_load):
        """Test summary with two profitable trades."""
        from trade_journal import get_journal_summary

        # Arrange - two sell-first profitable trades
        mock_load.return_value = [
            {"symbol": "SYM1", "transaction_type": "SELL",
             "price": 100, "quantity": 50, "timestamp": "2026-02-12T10:00:00",
             "algo_source": "Trending_Market_Code", "option_type": "PE",
             "trade_date": "2026-02-12"},
            {"symbol": "SYM1", "transaction_type": "BUY",
             "price": 80, "quantity": 50, "timestamp": "2026-02-12T11:00:00",
             "algo_source": "Unknown", "option_type": "PE",
             "trade_date": "2026-02-12"},
        ]

        # Act
        result = get_journal_summary(date(2026, 2, 12), date(2026, 2, 12))

        # Assert
        assert result["total_pnl"] == 1000.0  # (100-80)*50
        assert result["algo_pnl"] == 1000.0  # algo sell → 100% algo
        assert result["manual_pnl"] == 0.0
        assert result["total_trades"] == 1
        assert result["wins"] == 1
        assert result["win_rate"] == 100.0
        assert "Survivor (NIFTY)" in result["by_algo"]

    @patch('trade_journal.load_orders_range')
    def test_empty_summary(self, mock_load):
        """Test summary with no orders returns zero values."""
        from trade_journal import get_journal_summary

        mock_load.return_value = []

        result = get_journal_summary(date(2026, 2, 12), date(2026, 2, 12))

        assert result["total_pnl"] == 0.0
        assert result["total_trades"] == 0
        assert result["win_rate"] == 0


# ============================================================================
# Test Trade Notes
# ============================================================================

class TestTradeNotes:
    """Tests for trade notes persistence."""

    @patch('trade_journal._get_notes_filepath')
    def test_save_and_load_note(self, mock_filepath):
        """Test saving and loading a trade note."""
        from trade_journal import save_trade_note, get_trade_notes

        temp_file = os.path.join(tempfile.mkdtemp(), "notes.json")
        mock_filepath.return_value = temp_file

        # Act
        result = save_trade_note(
            "NIFTY24JAN23000PE", "2026-02-12",
            "Sold expecting range-bound day",
            "Ranging", "Should have taken profit earlier"
        )

        # Assert
        assert result is True
        notes = get_trade_notes()
        key = "2026-02-12_NIFTY24JAN23000PE"
        assert key in notes
        assert notes[key]["note"] == "Sold expecting range-bound day"
        assert notes[key]["market_condition"] == "Ranging"
        assert notes[key]["lesson"] == "Should have taken profit earlier"

        # Cleanup
        os.unlink(temp_file)

    @patch('trade_journal._get_notes_filepath')
    def test_load_empty_notes(self, mock_filepath):
        """Test loading notes when no file exists."""
        from trade_journal import get_trade_notes

        mock_filepath.return_value = "/nonexistent/path/notes.json"

        result = get_trade_notes()

        assert result == {}


# ============================================================================
# Test Zerodha Reconciliation
# ============================================================================

class TestReconcileWithZerodha:
    """Tests for reconcile_with_zerodha function."""

    @patch('trade_journal.load_orders_for_date')
    @patch('common_lib.save_executed_order')
    def test_adds_missing_manual_trade(self, mock_save, mock_load):
        """Missing Zerodha fill is added with algo_source='Manual'."""
        from trade_journal import reconcile_with_zerodha
        from datetime import date

        mock_load.return_value = []  # Nothing recorded locally yet
        mock_save.return_value = True

        mock_kite = MagicMock()
        mock_kite.trades.return_value = [
            {
                "order_id": "999001",
                "tradingsymbol": "NIFTY2632422750PE",
                "transaction_type": "SELL",
                "quantity": 75,
                "average_price": 110.5,
                "fill_timestamp": "2026-03-20T10:30:00",
            }
        ]

        result = reconcile_with_zerodha(mock_kite, date(2026, 3, 20))

        assert result["added"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == []
        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args.kwargs
        assert call_kwargs["algo_source"] == "Manual"
        assert call_kwargs["order_id"] == "999001"
        assert call_kwargs["price"] == 110.5

    @patch('trade_journal.load_orders_for_date')
    @patch('common_lib.save_executed_order')
    def test_skips_already_recorded_by_order_id(self, mock_save, mock_load):
        """Trade already in local JSON (matched by order_id) is skipped."""
        from trade_journal import reconcile_with_zerodha
        from datetime import date

        mock_load.return_value = [
            {
                "order_id": "999001",
                "symbol": "NIFTY2632422750PE",
                "transaction_type": "SELL",
                "quantity": 75,
                "algo_source": "Trending_Market_Code",
            }
        ]

        mock_kite = MagicMock()
        mock_kite.trades.return_value = [
            {
                "order_id": "999001",
                "tradingsymbol": "NIFTY2632422750PE",
                "transaction_type": "SELL",
                "quantity": 75,
                "average_price": 110.5,
            }
        ]

        result = reconcile_with_zerodha(mock_kite, date(2026, 3, 20))

        assert result["added"] == 0
        assert result["skipped"] == 1
        mock_save.assert_not_called()

    @patch('trade_journal.load_orders_for_date')
    @patch('common_lib.save_executed_order')
    def test_skips_already_recorded_by_fuzzy_match(self, mock_save, mock_load):
        """Trade without order_id in local record is deduped by symbol+type+qty."""
        from trade_journal import reconcile_with_zerodha
        from datetime import date

        mock_load.return_value = [
            {
                "order_id": "",  # pre-fix record without order_id
                "symbol": "NIFTY2632422750PE",
                "transaction_type": "SELL",
                "quantity": 75,
                "algo_source": "Unknown",
            }
        ]

        mock_kite = MagicMock()
        mock_kite.trades.return_value = [
            {
                "order_id": "999001",
                "tradingsymbol": "NIFTY2632422750PE",
                "transaction_type": "SELL",
                "quantity": 75,
                "average_price": 110.5,
            }
        ]

        result = reconcile_with_zerodha(mock_kite, date(2026, 3, 20))

        assert result["added"] == 0
        assert result["skipped"] == 1
        mock_save.assert_not_called()

    @patch('trade_journal.load_orders_for_date')
    def test_handles_kite_api_failure_gracefully(self, mock_load):
        """kite.trades() failure returns error dict without raising."""
        from trade_journal import reconcile_with_zerodha
        from datetime import date

        mock_load.return_value = []
        mock_kite = MagicMock()
        mock_kite.trades.side_effect = Exception("Network timeout")

        result = reconcile_with_zerodha(mock_kite, date(2026, 3, 20))

        assert result["added"] == 0
        assert result["skipped"] == 0
        assert len(result["errors"]) == 1
        assert "Network timeout" in result["errors"][0]
