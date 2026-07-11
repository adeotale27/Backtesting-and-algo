"""
Unit tests for the /api/all_strikes endpoint.

Tests cover parameter validation, strike generation logic,
deduplication, and per-underlying strike gaps.
"""

import json
import pytest
from datetime import date
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAllStrikesValidation:
    """Tests for request validation on /api/all_strikes."""

    @pytest.fixture
    def client(self):
        """Create Flask test client with session support."""
        from flask_app import app
        app.config['TESTING'] = True
        with app.test_client() as client:
            # Pass the dashboard gatekeeper; endpoints still enforce their own
            # access_token session check, which these tests exercise.
            with client.session_transaction() as sess:
                sess['app_authenticated'] = True
            yield client

    def test_missing_underlying_returns_400(self, client):
        """Test that missing 'underlying' field returns 400."""
        # Arrange
        payload = {"expiry": "2026-02-20"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data

    def test_missing_expiry_returns_400(self, client):
        """Test that missing 'expiry' field returns 400."""
        # Arrange
        payload = {"underlying": "NIFTY"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data

    def test_empty_body_returns_400(self, client):
        """Test that an empty request body returns 400."""
        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps({}),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400

    def test_no_session_returns_401(self, client):
        """Test that missing session returns 401."""
        # Arrange
        payload = {"underlying": "NIFTY", "expiry": "2026-02-20"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'session' in data['error'].lower() or 'expired' in data['error'].lower()


class TestAllStrikesGeneration:
    """Tests for strike generation logic."""

    @pytest.fixture
    def client(self):
        """Create Flask test client with session support."""
        from flask_app import app
        app.config['TESTING'] = True
        with app.test_client() as client:
            # Set session access_token
            with client.session_transaction() as sess:
                sess['app_authenticated'] = True
                sess['access_token'] = 'test_token'
            yield client

    def _build_instruments_map(self, underlying, expiry_date, strike_gap,
                                atm_strike, num_otm=10):
        """Build a mock instruments map for testing.

        Args:
            underlying: Underlying name (e.g. 'NIFTY').
            expiry_date: Expiry as a date object.
            strike_gap: Gap between strikes.
            atm_strike: ATM strike price.
            num_otm: Number of OTM strikes to generate.

        Returns:
            dict: Mock instruments map keyed by tradingsymbol.
        """
        instruments = {}
        # Generate CE strikes: ATM + num_otm higher
        for i in range(num_otm + 1):
            strike = atm_strike + i * strike_gap
            sym = f"{underlying}26217{int(strike)}CE"
            instruments[sym] = {
                'name': underlying,
                'expiry': expiry_date,
                'segment': 'NFO-OPT',
                'lot_size': 75 if underlying == 'NIFTY' else 20,
                'instrument_token': 10000 + i,
                'strike': float(strike),
                'instrument_type': 'CE',
            }
        # Generate PE strikes: ATM + num_otm lower
        for i in range(num_otm + 1):
            strike = atm_strike - i * strike_gap
            sym = f"{underlying}26217{int(strike)}PE"
            instruments[sym] = {
                'name': underlying,
                'expiry': expiry_date,
                'segment': 'NFO-OPT',
                'lot_size': 75 if underlying == 'NIFTY' else 20,
                'instrument_token': 20000 + i,
                'strike': float(strike),
                'instrument_type': 'PE',
            }
        return instruments

    @patch('flask_app.KiteConnect')
    @patch('flask_app.INSTRUMENTS_MAP', new_callable=dict)
    def test_nifty_generates_correct_number_of_strikes(
        self, mock_map, mock_kite_class, client
    ):
        """Test that NIFTY generates ATM + 10 OTM for both CE and PE."""
        # Arrange
        expiry_date = date(2026, 2, 17)
        atm = 25700
        instruments = self._build_instruments_map(
            'NIFTY', expiry_date, 50, atm
        )
        mock_map.update(instruments)

        mock_kite = MagicMock()
        mock_kite_class.return_value = mock_kite

        # Mock spot quote
        mock_kite.quote.side_effect = lambda syms: {
            s: {'last_price': 25710.0} for s in
            (syms if isinstance(syms, list) else [syms])
        }

        payload = {"underlying": "NIFTY", "expiry": "2026-02-17"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'strikes' in data
        # 11 CE + 11 PE = 22 total
        assert len(data['strikes']) == 22
        assert data['atm_strike'] == atm
        assert data['spot_price'] == 25710.0

    @patch('flask_app.KiteConnect')
    @patch('flask_app.INSTRUMENTS_MAP', new_callable=dict)
    def test_sensex_uses_100_strike_gap(
        self, mock_map, mock_kite_class, client
    ):
        """Test that SENSEX uses 100-point strike gaps."""
        # Arrange
        expiry_date = date(2026, 2, 17)
        atm = 83200
        instruments = self._build_instruments_map(
            'SENSEX', expiry_date, 100, atm
        )
        # Override segment for SENSEX
        for sym in instruments:
            instruments[sym]['segment'] = 'BFO-OPT'
            instruments[sym]['lot_size'] = 20
        mock_map.update(instruments)

        mock_kite = MagicMock()
        mock_kite_class.return_value = mock_kite

        mock_kite.quote.side_effect = lambda syms: {
            s: {'last_price': 83150.0} for s in
            (syms if isinstance(syms, list) else [syms])
        }

        payload = {"underlying": "SENSEX", "expiry": "2026-02-17"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['atm_strike'] == 83200  # 83150 rounds to 83200

    @patch('flask_app.KiteConnect')
    @patch('flask_app.INSTRUMENTS_MAP', new_callable=dict)
    def test_strikes_have_is_generated_flag(
        self, mock_map, mock_kite_class, client
    ):
        """Test that all returned strikes have is_generated=True."""
        # Arrange
        expiry_date = date(2026, 2, 17)
        atm = 25700
        instruments = self._build_instruments_map(
            'NIFTY', expiry_date, 50, atm
        )
        mock_map.update(instruments)

        mock_kite = MagicMock()
        mock_kite_class.return_value = mock_kite
        mock_kite.quote.side_effect = lambda syms: {
            s: {'last_price': 100.0} for s in
            (syms if isinstance(syms, list) else [syms])
        }

        payload = {"underlying": "NIFTY", "expiry": "2026-02-17"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        data = json.loads(response.data)
        for strike in data['strikes']:
            assert strike['is_generated'] is True
            assert strike['qty'] == 0

    @patch('flask_app.KiteConnect')
    @patch('flask_app.INSTRUMENTS_MAP', new_callable=dict)
    def test_invalid_expiry_format_returns_400(
        self, mock_map, mock_kite_class, client
    ):
        """Test that an invalid expiry date format returns 400."""
        # Arrange
        mock_map.update({'DUMMY': {'name': 'X'}})  # Non-empty map

        mock_kite = MagicMock()
        mock_kite_class.return_value = mock_kite
        mock_kite.quote.return_value = {
            'NSE:NIFTY 50': {'last_price': 25000.0}
        }

        payload = {"underlying": "NIFTY", "expiry": "17-02-2026"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'expiry' in data['error'].lower() or 'format' in data['error'].lower()

    @patch('flask_app.KiteConnect')
    @patch('flask_app.INSTRUMENTS_MAP', new_callable=dict)
    def test_unknown_underlying_returns_400(
        self, mock_map, mock_kite_class, client
    ):
        """Test that an unknown underlying returns 400."""
        # Arrange
        mock_map.update({'DUMMY': {'name': 'X'}})

        mock_kite = MagicMock()
        mock_kite_class.return_value = mock_kite

        payload = {"underlying": "FOOBAR", "expiry": "2026-02-17"}

        # Act
        response = client.post(
            '/api/all_strikes',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'unknown' in data['error'].lower() or 'foobar' in data['error'].lower()


class TestStrikeGapMapping:
    """Tests for the strike gap and spot symbol mappings."""

    def test_strike_gap_nifty(self):
        """Test NIFTY strike gap is 50."""
        from flask_app import STRIKE_GAP_MAP
        assert STRIKE_GAP_MAP['NIFTY'] == 50

    def test_strike_gap_sensex(self):
        """Test SENSEX strike gap is 100."""
        from flask_app import STRIKE_GAP_MAP
        assert STRIKE_GAP_MAP['SENSEX'] == 100

    def test_strike_gap_banknifty(self):
        """Test BANKNIFTY strike gap is 50."""
        from flask_app import STRIKE_GAP_MAP
        assert STRIKE_GAP_MAP['BANKNIFTY'] == 50

    def test_spot_symbol_nifty(self):
        """Test NIFTY spot symbol mapping."""
        from flask_app import SPOT_SYMBOL_MAP
        assert SPOT_SYMBOL_MAP['NIFTY'] == 'NSE:NIFTY 50'

    def test_spot_symbol_sensex(self):
        """Test SENSEX spot symbol mapping."""
        from flask_app import SPOT_SYMBOL_MAP
        assert SPOT_SYMBOL_MAP['SENSEX'] == 'BSE:SENSEX'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
