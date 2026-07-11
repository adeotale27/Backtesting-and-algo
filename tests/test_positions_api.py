"""
Unit tests for NIFTY positions dashboard API endpoints.

Tests cover authentication, data structure validation, and filtering logic.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPositionsLib:
    """Tests for positions_lib module."""

    def test_calculate_margin_requirement_spread_only(self):
        """Test margin calculation with only spreads."""
        from positions_lib import calculate_margin_requirement
        
        # Arrange
        spread_count = 65  # 1 lot equivalent
        single_pe_ce = 0
        both_ce_pe = 0
        
        # Act
        margin = calculate_margin_requirement(spread_count, single_pe_ce, both_ce_pe)
        
        # Assert
        expected = (65 * 43000) / 65  # MARGIN_SPREAD / LOT_SIZE
        assert margin == expected

    def test_calculate_margin_requirement_mixed(self):
        """Test margin calculation with mixed positions."""
        from positions_lib import calculate_margin_requirement
        
        # Arrange
        spread_count = 65
        single_pe_ce = 65
        both_ce_pe = 65
        
        # Act
        margin = calculate_margin_requirement(spread_count, single_pe_ce, both_ce_pe)
        
        # Assert
        expected = (65 * 43000 + 65 * 162000 + 65 * 222000) / 65
        assert margin == expected

    def test_format_inr(self):
        """Test INR formatting function."""
        from positions_lib import format_inr
        
        # Arrange & Act & Assert
        assert format_inr(1234567.89) == "12,34,567.89"
        assert format_inr(1000) == "1,000"
        assert format_inr(100) == "100"


class TestPositionsAPI:
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


    def test_positions_dashboard_route_exists(self, client):
        """Test that /positions route exists and returns HTML."""
        # Act
        response = client.get('/positions')
        
        # Assert
        assert response.status_code == 200
        assert b'NIFTY Positions Dashboard' in response.data

    def test_nifty_positions_requires_auth(self, client):
        """Test that API requires authentication."""
        # Act
        response = client.post(
            '/api/nifty_positions',
            data=json.dumps({}),
            content_type='application/json'
        )
        
        # Assert
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'error' in data

    def test_next_expiry_positions_requires_auth(self, client):
        """Test that next expiry API requires authentication."""
        # Act
        response = client.post(
            '/api/nifty_positions/next_expiry',
            data=json.dumps({}),
            content_type='application/json'
        )
        
        # Assert
        assert response.status_code == 401
        data = json.loads(response.data)
        assert 'error' in data

    def test_custom_days_positions_requires_days(self, client):
        """Test that custom days API requires valid days parameter."""
        # Act
        response = client.post(
            '/api/nifty_positions/custom_days',
            data=json.dumps({'request_token': 'test'}),
            content_type='application/json'
        )
        
        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'days' in data['error'].lower()

    def test_custom_days_positions_rejects_invalid_days(self, client):
        """Test that custom days API rejects non-positive days."""
        # Act
        response = client.post(
            '/api/nifty_positions/custom_days',
            data=json.dumps({'request_token': 'test', 'days': 0}),
            content_type='application/json'
        )
        
        # Assert
        assert response.status_code == 400

    def test_custom_days_positions_rejects_negative_days(self, client):
        """Test that custom days API rejects negative days."""
        # Act
        response = client.post(
            '/api/nifty_positions/custom_days',
            data=json.dumps({'request_token': 'test', 'days': -5}),
            content_type='application/json'
        )
        
        # Assert
        assert response.status_code == 400


class TestPositionsSummary:
    """Tests for get_nifty_positions_summary function."""

    @patch('positions_lib.mibian')
    def test_returns_expected_structure(self, mock_mibian):
        """Test that position summary returns expected data structure."""
        from positions_lib import get_nifty_positions_summary
        
        # Arrange
        mock_kite = MagicMock()
        mock_kite.instruments.return_value = []
        mock_kite.positions.return_value = {'net': []}
        mock_kite.quote.return_value = {'NSE:NIFTY 50': {'last_price': 23000}}
        
        # Act
        result = get_nifty_positions_summary(mock_kite)
        
        # Assert
        assert 'ce_sold' in result
        assert 'pe_sold' in result
        assert 'total_delta' in result
        assert 'margin_required' in result
        assert 'margin_formatted' in result
        assert 'today_ce_qty' in result
        assert 'today_pe_qty' in result
        assert 'today_delta' in result
        assert 'today_margin' in result
        assert 'today_margin_formatted' in result
        assert 'positions' in result
        assert isinstance(result['positions'], list)

    @patch('positions_lib.mibian')
    def test_empty_positions_returns_zeros(self, mock_mibian):
        """Test that empty positions return zero values."""
        from positions_lib import get_nifty_positions_summary
        
        # Arrange
        mock_kite = MagicMock()
        mock_kite.instruments.return_value = []
        mock_kite.positions.return_value = {'net': []}
        mock_kite.quote.return_value = {'NSE:NIFTY 50': {'last_price': 23000}}
        
        # Act
        result = get_nifty_positions_summary(mock_kite)
        
        # Assert
        assert result['ce_sold'] == 0
        assert result['pe_sold'] == 0
        assert result['total_delta'] == 0
        assert result['margin_required'] == 0
        assert result['today_ce_qty'] == 0
        assert result['today_pe_qty'] == 0
        assert result['today_delta'] == 0
        assert result['today_margin'] == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
