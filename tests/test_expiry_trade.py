"""
Unit tests for the Expiry Trade System.

Tests cover expiry detection, common substring extraction,
Stochastic RSI calculation, support/resistance, tick health,
and Flask API endpoints.
"""

import datetime
import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
import os
from zoneinfo import ZoneInfo

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# expiry_trade_lib imports pandas_ta at module level, which needs Python >= 3.12.
# Skip the whole file (instead of erroring) on environments that can't have it,
# e.g. the GCP VM's Python 3.11 venv.
pytest.importorskip("pandas_ta", reason="pandas_ta unavailable (requires Python >= 3.12)")

_IST = ZoneInfo("Asia/Kolkata")


def _ist_now() -> datetime.datetime:
    """IST-aware now — matches expiry_trade_lib's get_ist_now() tick timestamps."""
    return datetime.datetime.now(_IST)


class TestExpiryDetection:
    """Tests for expiry detection logic."""

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_detect_nifty_expiry_today(self, mock_kc_cls, mock_kt_cls):
        """Test that NIFTY expiry is detected when options expire today."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        today = datetime.date.today()
        mock_kite.instruments.return_value = [
            {
                "tradingsymbol": "NIFTY2621923000CE",
                "expiry": today,
                "instrument_type": "CE",
            },
            {
                "tradingsymbol": "NIFTY2621923000PE",
                "expiry": today,
                "instrument_type": "PE",
            },
            {
                "tradingsymbol": "NIFTY2621923100CE",
                "expiry": today,
                "instrument_type": "CE",
            },
        ]
        system = ExpiryTradeSystem(kite=mock_kite)

        # Act
        result = system.detect_expiry()

        # Assert
        assert result is True
        assert system.active_index == "NIFTY"
        assert system.expiry_substring is not None
        assert system.expiry_substring.startswith("NIFTY")

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_detect_sensex_expiry_today(self, mock_kc_cls, mock_kt_cls):
        """Test that SENSEX expiry is detected when only SENSEX expires."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        # NIFTY expires tomorrow, SENSEX expires today
        nifty_instruments = [
            {
                "tradingsymbol": "NIFTY2621923000CE",
                "expiry": tomorrow,
                "instrument_type": "CE",
            },
        ]
        sensex_instruments = [
            {
                "tradingsymbol": "SENSEX2621970000CE",
                "expiry": today,
                "instrument_type": "CE",
            },
            {
                "tradingsymbol": "SENSEX2621970000PE",
                "expiry": today,
                "instrument_type": "PE",
            },
            {
                "tradingsymbol": "SENSEX2621970100CE",
                "expiry": today,
                "instrument_type": "CE",
            },
        ]
        mock_kite.instruments.side_effect = (
            lambda exchange: nifty_instruments
            if exchange == "NFO"
            else sensex_instruments
        )
        system = ExpiryTradeSystem(kite=mock_kite)

        # Act
        result = system.detect_expiry()

        # Assert
        assert result is True
        assert system.active_index == "SENSEX"
        assert "SENSEX" in system.expiry_substring

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_no_expiry_today(self, mock_kc_cls, mock_kt_cls):
        """Test that no expiry is detected when nothing expires today."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        mock_kite.instruments.return_value = [
            {
                "tradingsymbol": "NIFTY2621923000CE",
                "expiry": tomorrow,
                "instrument_type": "CE",
            },
        ]
        system = ExpiryTradeSystem(kite=mock_kite)

        # Act
        result = system.detect_expiry()

        # Assert
        assert result is False
        assert system.active_index is None
        assert system.expiry_substring is None


class TestCommonSubstring:
    """Tests for _extract_common_substring static method."""

    def test_common_prefix_nifty(self):
        """Test common prefix extraction for NIFTY symbols."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        symbols = [
            "NIFTY2621923000CE",
            "NIFTY2621923000PE",
            "NIFTY2621923100CE",
            "NIFTY2621915000PE",
            "NIFTY2621924200CE",
        ]

        # Act
        result = ExpiryTradeSystem._extract_common_substring(symbols)

        # Assert
        assert result == "NIFTY26219"

    def test_common_prefix_sensex(self):
        """Test common prefix extraction for SENSEX symbols."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        symbols = [
            "SENSEX2621970000CE",
            "SENSEX2621970000PE",
            "SENSEX2621970100CE",
            "SENSEX2621968000PE",
        ]

        # Act
        result = ExpiryTradeSystem._extract_common_substring(symbols)

        # Assert
        assert result == "SENSEX26219"

    def test_empty_symbols(self):
        """Test that empty list returns empty string."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Act & Assert
        assert ExpiryTradeSystem._extract_common_substring([]) == ""

    def test_single_symbol(self):
        """Test that single symbol returns the full symbol."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Act
        result = ExpiryTradeSystem._extract_common_substring(
            ["NIFTY2621923000CE"]
        )

        # Assert
        assert result == "NIFTY2621923000CE"


class TestStochasticRSI:
    """Tests for Stochastic RSI calculation."""

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_stoch_rsi_with_sufficient_data(self, mock_kc_cls, mock_kt_cls):
        """Test StochRSI produces results with enough candles."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)

        # Generate 50 candles — use today's date for output filtering
        today = datetime.date.today()
        base_time = datetime.datetime.combine(
            today, datetime.time(9, 15)
        )
        candles = []
        for i in range(50):
            t = base_time + datetime.timedelta(minutes=i * 3)
            price = 23000 + (i * 10) + ((-1) ** i * 5)
            candles.append(
                {
                    "time": t,
                    "open": price - 5,
                    "high": price + 10,
                    "low": price - 10,
                    "close": price,
                }
            )
        system._candles = candles

        # Act
        system._recalculate_stoch_rsi()

        # Assert
        assert len(system._stoch_rsi) > 0
        for point in system._stoch_rsi:
            assert "time" in point
            assert "k" in point
            assert "d" in point
            assert 0 <= point["k"] <= 100
            assert 0 <= point["d"] <= 100

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_stoch_rsi_insufficient_data(self, mock_kc_cls, mock_kt_cls):
        """Test StochRSI returns empty list with insufficient candles."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)
        system._candles = [
            {
                "time": datetime.datetime.now(),
                "open": 100,
                "high": 105,
                "low": 95,
                "close": 102,
            }
        ] * 30  # Only 30 candles, need 35+

        # Act
        system._recalculate_stoch_rsi()

        # Assert
        assert system._stoch_rsi == []


class TestSupportResistance:
    """Tests for 1-hour support/resistance calculation."""

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_sr_from_historical_data(self, mock_kc_cls, mock_kt_cls):
        """Test S/R levels are correctly extracted from 1-hr candles."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        mock_kite.historical_data.return_value = [
            {
                "date": datetime.datetime(2026, 2, 19, 9, 15),
                "open": 23000,
                "high": 23150,
                "low": 22950,
                "close": 23080,
            },
            {
                "date": datetime.datetime(2026, 2, 19, 10, 15),
                "open": 23080,
                "high": 23200,
                "low": 23050,
                "close": 23180,
            },
        ]
        system = ExpiryTradeSystem(kite=mock_kite)
        system._spot_instrument_token = 12345

        # Act
        system._fetch_support_resistance()

        # Assert
        assert len(system._support_resistance) == 2
        assert system._support_resistance[0]["support"] == 22950
        assert system._support_resistance[0]["resistance"] == 23150
        assert system._support_resistance[1]["support"] == 23050
        assert system._support_resistance[1]["resistance"] == 23200


class TestTickHealth:
    """Tests for tick health monitoring."""

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_is_stuck_no_ticks(self, mock_kc_cls, mock_kt_cls):
        """Test system reports stuck when no tick has ever arrived."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)

        # Act & Assert
        assert system.is_stuck is True

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_is_stuck_old_tick(self, mock_kc_cls, mock_kt_cls):
        """Test system reports stuck when last tick is over 10s ago."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)
        system._last_tick_time = _ist_now() - datetime.timedelta(seconds=15)

        # Act & Assert
        assert system.is_stuck is True

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_not_stuck_recent_tick(self, mock_kc_cls, mock_kt_cls):
        """Test system is not stuck when tick is recent."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)
        system._last_tick_time = _ist_now()

        # Act & Assert
        assert system.is_stuck is False


class TestCandleAggregation:
    """Tests for 3-minute candle aggregation."""

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_candle_bucket_rounding(self, mock_kc_cls, mock_kt_cls):
        """Test that candle bucket rounds down to 3-min boundary."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange & Act
        dt1 = datetime.datetime(2026, 2, 19, 9, 17, 30)
        dt2 = datetime.datetime(2026, 2, 19, 9, 15, 0)
        dt3 = datetime.datetime(2026, 2, 19, 9, 20, 59)

        # Assert
        bucket1 = ExpiryTradeSystem._get_candle_bucket(dt1)
        assert bucket1 == datetime.datetime(2026, 2, 19, 9, 15, 0)

        bucket2 = ExpiryTradeSystem._get_candle_bucket(dt2)
        assert bucket2 == datetime.datetime(2026, 2, 19, 9, 15, 0)

        bucket3 = ExpiryTradeSystem._get_candle_bucket(dt3)
        assert bucket3 == datetime.datetime(2026, 2, 19, 9, 18, 0)

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_update_candle_creates_new(self, mock_kc_cls, mock_kt_cls):
        """Test that _update_candle creates a new candle on first tick."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)

        # Act
        system._update_candle(23000)

        # Assert
        assert system._current_candle is not None
        assert system._current_candle["open"] == 23000
        assert system._current_candle["high"] == 23000
        assert system._current_candle["low"] == 23000
        assert system._current_candle["close"] == 23000

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_update_candle_updates_ohlc(self, mock_kc_cls, mock_kt_cls):
        """Test that subsequent ticks update high/low/close correctly."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)
        system._update_candle(23000)

        # Act
        system._update_candle(23050)  # New high
        system._update_candle(22980)  # New low
        system._update_candle(23020)  # Close

        # Assert
        assert system._current_candle["open"] == 23000
        assert system._current_candle["high"] == 23050
        assert system._current_candle["low"] == 22980
        assert system._current_candle["close"] == 23020


class TestGetStatus:
    """Tests for the get_status method."""

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_status_when_inactive(self, mock_kc_cls, mock_kt_cls):
        """Test status dict when system is not active."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)

        # Act
        status = system.get_status()

        # Assert
        assert status["is_active"] is False
        assert status["is_stuck"] is False
        assert status["active_index"] is None
        assert status["expiry_substring"] is None
        assert status["last_tick_time"] is None

    @patch("expiry_trade_lib.KiteTicker")
    @patch("expiry_trade_lib.KiteConnect")
    def test_status_when_active(self, mock_kc_cls, mock_kt_cls):
        """Test status dict when system is running."""
        from expiry_trade_lib import ExpiryTradeSystem

        # Arrange
        mock_kite = MagicMock()
        system = ExpiryTradeSystem(kite=mock_kite)
        system._is_active = True
        system._active_index = "SENSEX"
        system._expiry_substring = "SENSEX26219"
        system._last_tick_time = _ist_now()

        # Act
        status = system.get_status()

        # Assert
        assert status["is_active"] is True
        assert status["is_stuck"] is False
        assert status["active_index"] == "SENSEX"
        assert status["expiry_substring"] == "SENSEX26219"
        assert status["last_tick_time"] is not None


class TestFlaskEndpoints:
    """Tests for Flask API endpoints."""

    @pytest.fixture
    def client(self):
        """Create Flask test client."""
        from flask_app import app

        app.config["TESTING"] = True
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["app_authenticated"] = True
            yield client

    def test_expiry_trade_route_exists(self, client):
        """Test that /expiry_trade route exists and returns HTML."""
        # Act
        response = client.get("/expiry_trade")

        # Assert
        assert response.status_code == 200
        assert b"Expiry Trade Dashboard" in response.data

    def test_status_returns_json(self, client):
        """Test that /api/expiry_trade/status returns valid JSON."""
        # Act
        response = client.get("/api/expiry_trade/status")

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "is_active" in data
        assert "active_index" in data

    def test_candles_returns_json(self, client):
        """Test that /api/expiry_trade/candles returns valid JSON."""
        # Act
        response = client.get("/api/expiry_trade/candles")

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "candles" in data
        assert isinstance(data["candles"], list)

    def test_stoch_rsi_returns_json(self, client):
        """Test that /api/expiry_trade/stoch_rsi returns valid JSON."""
        # Act
        response = client.get("/api/expiry_trade/stoch_rsi")

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "stoch_rsi" in data
        assert isinstance(data["stoch_rsi"], list)

    def test_support_resistance_returns_json(self, client):
        """Test that /api/expiry_trade/support_resistance returns valid JSON."""
        # Act
        response = client.get("/api/expiry_trade/support_resistance")

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "support_resistance" in data
        assert isinstance(data["support_resistance"], list)

    def test_start_requires_token_or_session(self, client):
        """Test that /api/expiry_trade/start rejects an unauthenticated empty request.

        The endpoint returns 401 (Authentication required) when neither a
        request_token nor a Kite access_token session is present.
        """
        # Act
        response = client.post(
            "/api/expiry_trade/start",
            data=json.dumps({}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 401
        data = json.loads(response.data)
        assert data["success"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
