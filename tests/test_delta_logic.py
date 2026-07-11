
import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch

# Add parent dir to path to import common_lib
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock kite and other globals before importing common_lib if needed
# But common_lib imports might need them. 
# Strategy: Mock common_lib's dependencies AFTER import or patch them.

class TestDeltaLogic(unittest.TestCase):

    def setUp(self):
        # Import inside setup to avoid global execution issues if any
        import common_lib
        self.lib = common_lib
        
        # Setup Globals
        self.lib.all_instruments = {
            123: {'instrument_token': 123, 'tradingsymbol': 'NIFTY23DEC19000CE', 'expiry': '2023-12-28', 'segment': 'NFO-OPT', 'instrument_type': 'CE', 'strike': 19000, 'days_to_expiry': 5},
            124: {'instrument_token': 124, 'tradingsymbol': 'NIFTY23DEC19000PE', 'expiry': '2023-12-28', 'segment': 'NFO-OPT', 'instrument_type': 'PE', 'strike': 19000, 'days_to_expiry': 5},
            456: {'instrument_token': 456, 'tradingsymbol': 'BANKNIFTY23DEC43000CE', 'expiry': '2023-12-28', 'segment': 'NFO-OPT', 'instrument_type': 'CE', 'strike': 43000, 'days_to_expiry': 5}
        }
        self.lib.token_symbol_map = {'NIFTY23DEC19000CE': 123, 'BANKNIFTY23DEC43000CE': 456}
        self.lib.todays_volatility = 15
        self.lib.interest_rate = 10
        self.lib.delta_calculation_days = 30
        self.lib.exchange = "NFO"
        
        # Mock Delta limits
        self.lib.delta_limits_config = {
            "NIFTY": {
                "2023-12-28": {"min": -100, "max": 100},
                "default": {"min": -500, "max": 500}
            }
        }
        
    @patch('common_lib.get_cached_positions')
    @patch('common_lib.get_nifty_current_quote')
    @patch('greeks_lib.BS')
    def test_get_nifty_current_greeks_breakdown(self, mock_bs, mock_quote, mock_positions):
        # Mock mibian BS
        mock_instance = MagicMock()
        mock_instance.callDelta = 0.5
        mock_instance.putDelta = -0.4
        mock_bs.return_value = mock_instance
        
        # Mock Quote
        mock_quote.return_value = {'last_price': 19050}
        
        # Mock Positions
        mock_positions.return_value = {
            'net': [
                {'instrument_token': 123, 'tradingsymbol': 'NIFTY23DEC19000CE', 'quantity': 100},  # Delta +50
                {'instrument_token': 124, 'tradingsymbol': 'NIFTY23DEC19000PE', 'quantity': 50}    # Delta -20
            ]
        }
        
        greeks = self.lib.get_nifty_current_greeks()
        
        # Total Delta: (100 * 0.5) + (50 * -0.4) = 50 - 20 = 30
        self.assertEqual(greeks['delta'], 30.0)
        
        # Check Expiry Map
        self.assertIn('2023-12-28', greeks['expiry_delta'])
        self.assertEqual(greeks['expiry_delta']['2023-12-28'], 30.0)

    @patch('common_lib.get_nifty_current_greeks')
    @patch('common_lib.get_bank_nifty_current_greeks')
    @patch('common_lib.reset_restrictions')
    def test_set_restrictions_specific_expiry(self, mock_reset, mock_bn_greeks, mock_nifty_greeks):
        
        # Setup Reset mock
        mock_reset.return_value = {
            'nifty': {'futures': {'buy': 'yes', 'sell': 'yes'}, 'ce': {'buy': 'yes', 'sell': 'yes'}, 'pe': {'buy': 'yes', 'sell': 'yes'}},
            'bank_nifty': {'futures': {'buy': 'yes', 'sell': 'yes'}, 'ce': {'buy': 'yes', 'sell': 'yes'}, 'pe': {'buy': 'yes', 'sell': 'yes'}}
        }
        
        # Setup Greeks mock
        mock_nifty_greeks.return_value = {
            'delta': 200, 
            'expiry_delta': {'2023-12-28': 200}
        }
        mock_bn_greeks.return_value = {'delta': 0, 'expiry_delta': {}}
        
        # Test Case 1: Symbol matches expiry, Delta (200) > Max (100) -> Should Restrict
        self.lib.symbol = "NIFTY23DEC19000CE"
        
        # Update specific limit to be stricter for this test
        self.lib.delta_limits_config["NIFTY"]["2023-12-28"] = {"min": -100, "max": 100}
        
        restrictions = self.lib.set_restrictions()
        
        nifty_restr = restrictions['nifty']
        # Expect restriction because 200 > 100
        self.assertEqual(nifty_restr['futures']['buy'], "no") # Restricted
        
    @patch('common_lib.get_nifty_current_greeks')
    @patch('common_lib.get_bank_nifty_current_greeks')
    @patch('common_lib.reset_restrictions')
    def test_set_restrictions_default_limit(self, mock_reset, mock_bn_greeks, mock_nifty_greeks):
        
        mock_reset.return_value = {
            'nifty': {'futures': {'buy': 'yes', 'sell': 'yes'}, 'ce': {'buy': 'yes', 'sell': 'yes'}, 'pe': {'buy': 'yes', 'sell': 'yes'}},
            'bank_nifty': {'futures': {'buy': 'yes', 'sell': 'yes'}, 'ce': {'buy': 'yes', 'sell': 'yes'}, 'pe': {'buy': 'yes', 'sell': 'yes'}}
        }
        
        # Case 2: Expiry delta is 200, Default Limit is 500. Specific limit removed.
        self.lib.delta_limits_config["NIFTY"].pop("2023-12-28", None)
        
        mock_nifty_greeks.return_value = {
            'delta': 200, 
            'expiry_delta': {'2023-12-28': 200}
        }
        mock_bn_greeks.return_value = {'delta': 0, 'expiry_delta': {}}
        
        self.lib.symbol = "NIFTY23DEC19000CE" # Has Expiry 2023-12-28
        
        restrictions = self.lib.set_restrictions()
        nifty_restr = restrictions['nifty']
        
        # Expect NO restriction because 200 < 500 (Default)
        self.assertEqual(nifty_restr['futures']['buy'], "yes") 

if __name__ == '__main__':
    unittest.main()
