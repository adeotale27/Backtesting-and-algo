import datetime
import pytest
from unittest.mock import MagicMock
import sys
import os

# Add the directory to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/../")

from positions_lib import get_nifty_positions_summary

def test_theta_calculation():
    """Test that theta is calculated and included in the summary."""
    mock_kite = MagicMock()
    
    # Mock instruments: 1 CE and 1 PE, both expiring in 5 days
    today = datetime.date.today()
    expiry = today + datetime.timedelta(days=5)
    
    mock_kite.instruments.return_value = [
        {
            "instrument_token": 123,
            "tradingsymbol": "NIFTY24JAN23000CE",
            "name": "NIFTY",
            "expiry": expiry,
            "strike": 23000,
            "instrument_type": "CE",
            "segment": "NFO-OPT",
            "exchange": "NFO",
        },
        {
            "instrument_token": 124,
            "tradingsymbol": "NIFTY24JAN23000PE",
            "name": "NIFTY",
            "expiry": expiry,
            "strike": 23000,
            "instrument_type": "PE",
            "segment": "NFO-OPT",
            "exchange": "NFO",
        }
    ]
    
    # Mock positions: Short 1 lot CE, Short 1 lot PE
    mock_kite.positions.return_value = {
        "net": [
            {
                "instrument_token": 123,
                "tradingsymbol": "NIFTY24JAN23000CE",
                "quantity": -50,
                "average_price": 100,
                "last_price": 95,
                "pnl": 250,
                "day_buy_quantity": 0,
                "day_sell_quantity": 0,
            },
            {
                "instrument_token": 124,
                "tradingsymbol": "NIFTY24JAN23000PE",
                "quantity": -50,
                "average_price": 100,
                "last_price": 95,
                "pnl": 250,
                "day_buy_quantity": 0,
                "day_sell_quantity": 0,
            }
        ]
    }
    
    # Mock quote
    mock_kite.quote.return_value = {
        "NSE:NIFTY 50": {"last_price": 23000}
    }
    
    summary = get_nifty_positions_summary(mock_kite)
    
    # Assertions
    assert "total_theta" in summary
    assert "ce_theta" in summary
    assert "pe_theta" in summary
    assert "expiry_theta_map" in summary
    
    # Theta for a short position should be positive (decay is gain)
    assert summary["total_theta"] > 0
    assert summary["ce_theta"] > 0
    assert summary["pe_theta"] > 0
    
    # Check individual position theta
    for pos in summary["positions"]:
        assert "theta" in pos
        assert pos["theta"] > 0

from unittest.mock import patch

def test_calendar_days_logic():
    """Test that days_to_expiry uses calendar days."""
    mock_kite = MagicMock()
    
    # Today is Wednesday (2026-04-01)
    # Expiry is next Monday (2026-04-06)
    # Calendar days = 5
    
    today = datetime.date(2026, 4, 1)
    expiry = datetime.date(2026, 4, 6)
    
    with patch('datetime.date') as mock_date:
        mock_date.today.return_value = today
        mock_kite.instruments.return_value = [
            {
                "instrument_token": 123,
                "tradingsymbol": "NIFTY24APR23000CE",
                "name": "NIFTY",
                "expiry": expiry,
                "strike": 23000,
                "instrument_type": "CE",
                "segment": "NFO-OPT",
                "exchange": "NFO",
            }
        ]
        
        mock_kite.positions.return_value = {"net": []}
        mock_kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 23000}}
        
        from positions_lib import get_all_nifty_instruments
        instruments = get_all_nifty_instruments(mock_kite)
        
        assert instruments[123]["days_to_expiry"] == 5


if __name__ == "__main__":
    pytest.main([__file__])
