"""
Unit tests for SENSEX positions dashboard API endpoints.

Tests cover authentication, data structure validation, and filtering logic.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSensexPositionsLib:
    """Tests for sensex_positions_lib module."""

    def test_calculate_margin_requirement_spread_only(self):
        """Test margin calculation with only spreads."""
        from sensex_positions_lib import calculate_margin_requirement, SENSEX_LOT_SIZE, MARGIN_SPREAD
        
        # Arrange
        spread_count = 75  # arbitrary qty
        single_pe_ce = 0
        both_ce_pe = 0
        
        # Act
        margin = calculate_margin_requirement(spread_count, single_pe_ce, both_ce_pe)
        
        # Assert
        expected = (spread_count * MARGIN_SPREAD) / SENSEX_LOT_SIZE
        assert margin == expected

    def test_format_inr(self):
        """Test INR formatting function."""
        from sensex_positions_lib import format_inr
        
        # Arrange & Act & Assert
        assert format_inr(1234567.89) == "12,34,567.89"
        assert format_inr(1000) == "1,000"
        assert format_inr(100) == "100"


class TestSensexPositionsAPI:
    """Tests for Flask API endpoints."""

    @pytest.fixture
    def client(self):
        """Create Flask test client."""
        from flask_app import app
        app.config['TESTING'] = True
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['app_authenticated'] = True
            yield client

    def test_positions_dashboard_sensex_route_exists(self, client):
        """Test that /sensex_positions route exists and returns HTML."""
        # Act
        response = client.get('/sensex_positions')
        
        # Assert
        assert response.status_code == 200
        assert b'SENSEX Positions Dashboard' in response.data

    def test_sensex_positions_requires_auth(self, client):
        """Test that API requires authentication."""
        # Act
        response = client.post(
            '/api/sensex_positions',
            data=json.dumps({}),
            content_type='application/json'
        )

        # Assert: no request_token and no session access_token -> 401 (auth required).
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'error' in data

    def test_next_expiry_positions_requires_auth(self, client):
        """Test that next expiry API requires authentication."""
        # Act
        response = client.post(
            '/api/sensex_positions/next_expiry',
            data=json.dumps({}),
            content_type='application/json'
        )

        # Assert: no request_token and no session access_token -> 401 (auth required).
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'error' in data

    def test_custom_days_positions_requires_days(self, client):
        """Test that custom days API requires valid days parameter."""
        # Act
        response = client.post(
            '/api/sensex_positions/custom_days',
            data=json.dumps({'request_token': 'test'}),
            content_type='application/json'
        )
        
        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'days' in data['error'].lower()


class TestSensexPositionsSummary:
    """Tests for get_sensex_positions_summary function."""

    @patch('sensex_positions_lib.mibian')
    def test_returns_expected_structure(self, mock_mibian):
        """Test that position summary returns expected data structure."""
        from sensex_positions_lib import get_sensex_positions_summary
        
        # Arrange
        mock_kite = MagicMock()
        mock_kite.instruments.return_value = []
        mock_kite.positions.return_value = {'net': []}
        mock_kite.quote.return_value = {'BSE:SENSEX': {'last_price': 75000}}
        
        # Act
        result = get_sensex_positions_summary(mock_kite)
        
        # Assert
        assert 'ce_sold' in result
        assert 'pe_sold' in result
        assert 'total_delta' in result
        assert 'margin_required' in result
        assert 'margin_formatted' in result
        assert 'positions' in result
        assert 'sensex_spot' in result
        assert isinstance(result['positions'], list)

    @patch('sensex_positions_lib.mibian')
    def test_empty_positions_returns_zeros(self, mock_mibian):
        """Test that empty positions return zero values."""
        from sensex_positions_lib import get_sensex_positions_summary
        
        # Arrange
        mock_kite = MagicMock()
        mock_kite.instruments.return_value = []
        mock_kite.positions.return_value = {'net': []}
        mock_kite.quote.return_value = {'BSE:SENSEX': {'last_price': 75000}}
        
        # Act
        result = get_sensex_positions_summary(mock_kite)
        
        # Assert
        assert result['ce_sold'] == 0
        assert result['pe_sold'] == 0
        assert result['total_delta'] == 0
        assert result['margin_required'] == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
