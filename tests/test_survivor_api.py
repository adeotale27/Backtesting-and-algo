"""
Unit tests for Survivor Algo Dashboard API endpoints.

Tests the /api/survivor/start, /api/survivor/status, and /api/survivor/stop endpoints.
"""

import pytest
import json
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def client():
    """Create a test client for the Flask app."""
    from flask_app import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['app_authenticated'] = True
        yield client


class TestSurvivorStartEndpoint:
    """Tests for /api/survivor/start endpoint."""

    def test_start_nifty_sell_returns_success(self, client):
        """Test starting NIFTY sell-only instance returns success."""
        # Arrange
        payload = {
            "index_type": "NIFTY",
            "mode": "sell",
            "symbol_initials": "NIFTY26JAN",
            "distance": "200:200",
            "order_type": "NRML",
            "gap": "100:100",
            "reset_gap": "200:200",
            "quantity": "50:50",
            "start_points": "0:0",
            "request_token": "test_token_123"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "message" in data
        assert "place_order_at_nifty.py" in data["message"]
        assert "pid" in data

    def test_start_nifty_buy_returns_success(self, client):
        """Test starting NIFTY buy+sell instance returns success."""
        # Arrange
        payload = {
            "index_type": "NIFTY",
            "mode": "buy",
            "symbol_initials": "NIFTY26JAN",
            "distance": "200:200",
            "order_type": "NRML",
            "gap": "100:100",
            "reset_gap": "200:200",
            "quantity": "50:50",
            "start_points": "0:0",
            "request_token": "test_token_123"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "place_order_at_nifty_with_buy_auto.py" in data["message"]

    def test_start_sensex_sell_returns_success(self, client):
        """Test starting SENSEX sell-only instance returns success."""
        # Arrange
        payload = {
            "index_type": "SENSEX",
            "mode": "sell",
            "symbol_initials": "SENSEX26JAN",
            "distance": "500:500",
            "order_type": "NRML",
            "gap": "100:100",
            "reset_gap": "200:200",
            "quantity": "20:20",
            "start_points": "0:0",
            "request_token": "test_token_456"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "place_order_at_sensex.py" in data["message"]

    def test_start_sensex_buy_returns_success(self, client):
        """Test starting SENSEX buy+sell instance returns success."""
        # Arrange
        payload = {
            "index_type": "SENSEX",
            "mode": "buy",
            "symbol_initials": "SENSEX26JAN",
            "distance": "500:500",
            "order_type": "NRML",
            "gap": "100:100",
            "reset_gap": "200:200",
            "quantity": "20:20",
            "start_points": "0:0",
            "request_token": "test_token_789"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "place_order_at_sensex_with_buy_auto.py" in data["message"]

    def test_missing_parameters_returns_400(self, client):
        """Test that missing required parameters returns 400 error."""
        # Arrange
        payload = {
            "index_type": "NIFTY",
            "mode": "sell"
            # Missing other required fields
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data
        assert "Missing" in data["error"]


    def test_invalid_mode_returns_400(self, client):
        """Test that invalid mode returns 400 error."""
        # Arrange
        payload = {
            "index_type": "NIFTY",
            "mode": "invalid_mode",
            "symbol_initials": "NIFTY26JAN",
            "distance": "200:200",
            "order_type": "NRML",
            "gap": "100:100",
            "reset_gap": "200:200",
            "quantity": "50:50",
            "start_points": "0:0",
            "request_token": "test_token"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data
        assert "sell" in data["error"] or "buy" in data["error"]

    def test_start_stock_sell_returns_success(self, client):
        """Test starting NFO stock sell-only instance returns success."""
        # Arrange
        payload = {
            "index_type": "STOCK",
            "mode": "sell",
            "stock_name": "TCS",
            "symbol_initials": "TCS26FEB",
            "distance": "50:50",
            "order_type": "NRML",
            "gap": "5:5",
            "reset_gap": "10:10",
            "quantity": "175:175",
            "start_points": "0:0",
            "request_token": "test_token_stock_1"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "message" in data
        assert "place_order_at_stock.py" in data["message"]
        assert "pid" in data

    def test_start_stock_buy_returns_success(self, client):
        """Test starting NFO stock buy+sell instance returns success."""
        # Arrange
        payload = {
            "index_type": "STOCK",
            "mode": "buy",
            "stock_name": "RELIANCE",
            "symbol_initials": "RELIANCE26FEB",
            "distance": "100:100",
            "order_type": "NRML",
            "gap": "10:10",
            "reset_gap": "20:20",
            "quantity": "250:250",
            "start_points": "0:0",
            "request_token": "test_token_stock_2"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "place_order_at_stock_with_buy_auto.py" in data["message"]
        assert "pid" in data

    def test_start_stock_missing_stock_name_returns_400(self, client):
        """Test that STOCK type without stock_name returns 400."""
        # Arrange
        payload = {
            "index_type": "STOCK",
            "mode": "sell",
            "symbol_initials": "TCS26FEB",
            "distance": "50:50",
            "order_type": "NRML",
            "gap": "5:5",
            "reset_gap": "10:10",
            "quantity": "175:175",
            "start_points": "0:0",
            "request_token": "test_token"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data
        assert "stock_name" in data["error"]

    def test_invalid_index_type_returns_400(self, client):
        """Test that invalid index_type returns 400 error."""
        # Arrange
        payload = {
            "index_type": "INVALID",
            "mode": "sell",
            "symbol_initials": "TEST26JAN",
            "distance": "200:200",
            "order_type": "NRML",
            "gap": "100:100",
            "reset_gap": "200:200",
            "quantity": "50:50",
            "start_points": "0:0",
            "request_token": "test_token"
        }

        # Act
        response = client.post(
            '/api/survivor/start',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data
        assert "NIFTY" in data["error"] or "SENSEX" in data["error"] or "STOCK" in data["error"]



class TestSurvivorStatusEndpoint:
    """Tests for /api/survivor/status endpoint."""

    def test_status_returns_list(self, client):
        """Test that status endpoint returns a list."""
        # Act
        response = client.get('/api/survivor/status')

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert isinstance(data, list)


class TestSurvivorStopEndpoint:
    """Tests for /api/survivor/stop endpoint."""

    def test_stop_without_pid_returns_400(self, client):
        """Test that stopping without PID returns 400."""
        # Arrange
        payload = {}

        # Act
        response = client.post(
            '/api/survivor/stop',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data
        assert "PID" in data["error"]

    def test_stop_with_invalid_pid_returns_success(self, client):
        """Test that stopping with non-existent PID still returns success (graceful)."""
        # Arrange
        payload = {
            "pid": 99999999,  # Non-existent PID
            "status_file": "nonexistent.json"
        }

        # Act
        response = client.post(
            '/api/survivor/stop',
            data=json.dumps(payload),
            content_type='application/json'
        )

        # Assert
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "message" in data
