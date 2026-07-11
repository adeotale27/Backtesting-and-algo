"""
Unit tests for the /api/fetch_positions endpoint.

Verifies that positions are correctly grouped by underlying and expiry,
including fallback expiry extraction when instrument lookup cache misses.
"""

import json
import pytest
from datetime import date, datetime
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def client():
    """Create Flask test client with fresh session."""
    from flask_app import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["app_authenticated"] = True
        yield client


def _make_position(
    tradingsymbol: str,
    quantity: int,
    last_price: float = 100.0,
    average_price: float = 95.0,
    pnl: float = 0.0,
    product: str = "NRML",
    name: str = "",
    expiry: object = None,
) -> dict:
    """Helper to build a Kite-style position dict.

    Args:
        tradingsymbol: Trading symbol string.
        quantity: Net quantity (negative for short).
        last_price: Last traded price.
        average_price: Average entry price.
        pnl: Profit and loss value.
        product: Product type (NRML, MIS, etc.).
        name: Underlying name from Kite.
        expiry: Expiry date (date/datetime/str).

    Returns:
        dict: Position dictionary matching Kite API format.
    """
    pos = {
        "tradingsymbol": tradingsymbol,
        "quantity": quantity,
        "last_price": last_price,
        "average_price": average_price,
        "pnl": pnl,
        "product": product,
        "name": name,
    }
    if expiry is not None:
        pos["expiry"] = expiry
    return pos


def _make_instrument(
    name: str,
    expiry: object,
    lot_size: int = 75,
    instrument_token: int = 12345,
    strike: float = 23000,
    instrument_type: str = "PE",
    segment: str = "NFO-OPT",
) -> dict:
    """Helper to build an instrument lookup entry.

    Args:
        name: Underlying name.
        expiry: Expiry date.
        lot_size: Lot size.
        instrument_token: Instrument token.
        strike: Strike price.
        instrument_type: CE or PE.
        segment: Exchange segment.

    Returns:
        dict: Instrument metadata dictionary.
    """
    return {
        "name": name,
        "expiry": expiry,
        "lot_size": lot_size,
        "instrument_token": instrument_token,
        "strike": strike,
        "instrument_type": instrument_type,
        "segment": segment,
    }


class TestFetchPositionsGrouping:
    """Tests for position grouping logic in /api/fetch_positions."""

    @patch("flask_app.INSTRUMENTS_MAP", {})
    @patch("flask_app.get_instrument_lookup")
    @patch("flask_app.KiteConnect")
    def test_position_in_lookup_grouped_by_instrument_expiry(
        self, mock_kite_cls, mock_get_lookup, client
    ):
        """Position found in instrument cache uses cache expiry for grouping."""
        # Arrange
        mock_kite = MagicMock()
        mock_kite_cls.return_value = mock_kite
        mock_kite.generate_session.return_value = {"access_token": "test_token"}
        mock_kite.positions.return_value = {
            "net": [
                _make_position("NIFTY2631023450PE", -650, 90.25),
            ]
        }
        mock_get_lookup.return_value = {
            "NIFTY2631023450PE": _make_instrument(
                "NIFTY", date(2026, 3, 10), strike=23450
            )
        }

        # Act
        response = client.post(
            "/api/fetch_positions",
            data=json.dumps({"request_token": "test"}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        positions = data["positions"]
        assert "NIFTY" in positions
        assert "2026-03-10" in positions["NIFTY"]
        symbols = [p["symbol"] for p in positions["NIFTY"]["2026-03-10"]]
        assert "NIFTY2631023450PE" in symbols

    @patch("flask_app.INSTRUMENTS_MAP", {"EXISTING": {}})
    @patch("flask_app.get_instrument_lookup")
    @patch("flask_app.KiteConnect")
    def test_position_not_in_lookup_uses_position_expiry_date(
        self, mock_kite_cls, mock_get_lookup, client
    ):
        """Position NOT in instrument cache uses pos['expiry'] field as fallback."""
        # Arrange
        mock_kite = MagicMock()
        mock_kite_cls.return_value = mock_kite
        mock_kite.generate_session.return_value = {"access_token": "test_token"}
        # NIFTY2631023100PE is NOT in the lookup but has expiry in position data
        mock_kite.positions.return_value = {
            "net": [
                _make_position(
                    "NIFTY2631023100PE",
                    -325,
                    107.75,
                    name="NIFTY",
                    expiry=date(2026, 3, 10),
                ),
            ]
        }
        # Empty lookup — symbol not found
        mock_get_lookup.return_value = {}

        # Act
        response = client.post(
            "/api/fetch_positions",
            data=json.dumps({"request_token": "test"}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        positions = data["positions"]
        assert "NIFTY" in positions
        # Must be grouped under correct date, NOT "OTHERS"
        assert "OTHERS" not in positions["NIFTY"]
        assert "2026-03-10" in positions["NIFTY"]
        symbols = [p["symbol"] for p in positions["NIFTY"]["2026-03-10"]]
        assert "NIFTY2631023100PE" in symbols

    @patch("flask_app.INSTRUMENTS_MAP", {})
    @patch("flask_app.get_instrument_lookup")
    @patch("flask_app.KiteConnect")
    def test_position_not_in_lookup_uses_string_expiry(
        self, mock_kite_cls, mock_get_lookup, client
    ):
        """Position with string expiry in API data uses it correctly."""
        # Arrange
        mock_kite = MagicMock()
        mock_kite_cls.return_value = mock_kite
        mock_kite.generate_session.return_value = {"access_token": "test_token"}
        mock_kite.positions.return_value = {
            "net": [
                _make_position(
                    "NIFTY2631023100PE",
                    -325,
                    name="NIFTY",
                    expiry="2026-03-10",
                ),
            ]
        }
        mock_get_lookup.return_value = {}

        # Act
        response = client.post(
            "/api/fetch_positions",
            data=json.dumps({"request_token": "test"}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "2026-03-10" in data["positions"]["NIFTY"]

    @patch("flask_app.INSTRUMENTS_MAP", {})
    @patch("flask_app.get_instrument_lookup")
    @patch("flask_app.KiteConnect")
    def test_zero_quantity_positions_filtered(
        self, mock_kite_cls, mock_get_lookup, client
    ):
        """Positions with quantity == 0 are excluded from results."""
        # Arrange
        mock_kite = MagicMock()
        mock_kite_cls.return_value = mock_kite
        mock_kite.generate_session.return_value = {"access_token": "test_token"}
        mock_kite.positions.return_value = {
            "net": [
                _make_position("NIFTY2631023100PE", 0, name="NIFTY"),
            ]
        }
        mock_get_lookup.return_value = {}

        # Act
        response = client.post(
            "/api/fetch_positions",
            data=json.dumps({"request_token": "test"}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        # No NIFTY group should exist (only position was qty 0)
        assert data["positions"] == {}

    @patch("flask_app.INSTRUMENTS_MAP", {})
    @patch("flask_app.get_instrument_lookup")
    @patch("flask_app.KiteConnect")
    def test_mixed_lookup_hit_and_miss_all_grouped_correctly(
        self, mock_kite_cls, mock_get_lookup, client
    ):
        """Mix of cached and uncached positions all end up in correct expiry groups."""
        # Arrange
        mock_kite = MagicMock()
        mock_kite_cls.return_value = mock_kite
        mock_kite.generate_session.return_value = {"access_token": "test_token"}
        mock_kite.positions.return_value = {
            "net": [
                # Position IN lookup
                _make_position("NIFTY2631023450PE", -650, 90.25),
                # Position NOT in lookup, but has expiry in position data
                _make_position(
                    "NIFTY2631023100PE",
                    -325,
                    107.75,
                    name="NIFTY",
                    expiry=date(2026, 3, 10),
                ),
                # CE position also in lookup
                _make_position("NIFTY2631024000CE", -75, 50.0),
            ]
        }
        mock_get_lookup.return_value = {
            "NIFTY2631023450PE": _make_instrument(
                "NIFTY", date(2026, 3, 10), strike=23450
            ),
            "NIFTY2631024000CE": _make_instrument(
                "NIFTY",
                date(2026, 3, 10),
                strike=24000,
                instrument_type="CE",
            ),
        }

        # Act
        response = client.post(
            "/api/fetch_positions",
            data=json.dumps({"request_token": "test"}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        positions = data["positions"]
        assert "NIFTY" in positions
        assert "2026-03-10" in positions["NIFTY"]
        symbols = [p["symbol"] for p in positions["NIFTY"]["2026-03-10"]]
        # ALL three must appear under same expiry
        assert "NIFTY2631023450PE" in symbols
        assert "NIFTY2631023100PE" in symbols
        assert "NIFTY2631024000CE" in symbols
        # Nothing should be in OTHERS
        assert "OTHERS" not in positions["NIFTY"]

    @patch("flask_app.get_instrument_lookup")
    @patch("flask_app.KiteConnect")
    def test_cache_invalidated_when_missing_symbols(
        self, mock_kite_cls, mock_get_lookup, client
    ):
        """INSTRUMENTS_MAP is cleared when symbols are missing from cache."""
        # Arrange
        import flask_app

        flask_app.INSTRUMENTS_MAP = {"OLD_SYMBOL": {"name": "OLD"}}

        mock_kite = MagicMock()
        mock_kite_cls.return_value = mock_kite
        mock_kite.generate_session.return_value = {"access_token": "test_token"}
        mock_kite.positions.return_value = {
            "net": [
                _make_position(
                    "NIFTY2631023100PE",
                    -325,
                    name="NIFTY",
                    expiry=date(2026, 3, 10),
                ),
            ]
        }
        # Lookup returns a map that does NOT contain the position symbol
        mock_get_lookup.return_value = {"OTHER_SYMBOL": {"name": "OTHER"}}

        # Act
        response = client.post(
            "/api/fetch_positions",
            data=json.dumps({"request_token": "test"}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        # INSTRUMENTS_MAP should have been cleared
        assert flask_app.INSTRUMENTS_MAP == {}

    @patch("flask_app.INSTRUMENTS_MAP", {})
    @patch("flask_app.get_instrument_lookup")
    @patch("flask_app.KiteConnect")
    def test_lot_size_defaults_to_1_when_not_in_lookup(
        self, mock_kite_cls, mock_get_lookup, client
    ):
        """Position not in lookup should have lot_size=1 fallback."""
        # Arrange
        mock_kite = MagicMock()
        mock_kite_cls.return_value = mock_kite
        mock_kite.generate_session.return_value = {"access_token": "test_token"}
        mock_kite.positions.return_value = {
            "net": [
                _make_position(
                    "NIFTY2631023100PE",
                    -325,
                    name="NIFTY",
                    expiry=date(2026, 3, 10),
                ),
            ]
        }
        mock_get_lookup.return_value = {}

        # Act
        response = client.post(
            "/api/fetch_positions",
            data=json.dumps({"request_token": "test"}),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        pos = data["positions"]["NIFTY"]["2026-03-10"][0]
        assert pos["lot_size"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
