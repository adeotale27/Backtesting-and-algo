"""
Unit tests for order history tracking functionality.

Tests cover:
- save_executed_order: Saving orders to date-based JSON files
- load_todays_orders: Loading and sorting orders
- calculate_order_summary: Computing PE/CE summary with P&L
- _extract_symbol_info: Parsing symbol names for expiry and option type
"""

import pytest
import json
import os
import tempfile
from datetime import datetime
from unittest.mock import patch, MagicMock


class TestExtractSymbolInfo:
    """Tests for _extract_symbol_info function."""
    
    def test_extract_nifty_ce_symbol(self):
        """Test extraction from NIFTY CE symbol."""
        from common_lib import _extract_symbol_info
        result = _extract_symbol_info("NIFTY24JAN23000CE")
        assert result['expiry'] == '24JAN'
        assert result['option_type'] == 'CE'
    
    def test_extract_nifty_pe_symbol(self):
        """Test extraction from NIFTY PE symbol."""
        from common_lib import _extract_symbol_info
        result = _extract_symbol_info("NIFTY24JAN22500PE")
        assert result['expiry'] == '24JAN'
        assert result['option_type'] == 'PE'
    
    def test_extract_banknifty_symbol(self):
        """Test extraction from BANKNIFTY symbol."""
        from common_lib import _extract_symbol_info
        result = _extract_symbol_info("BANKNIFTY24FEB50000CE")
        assert result['expiry'] == '24FEB'
        assert result['option_type'] == 'CE'
    
    def test_extract_unknown_format(self):
        """Test fallback for unknown symbol format."""
        from common_lib import _extract_symbol_info
        result = _extract_symbol_info("RELIANCE")
        assert result['expiry'] == 'UNKNOWN'
        assert result['option_type'] == 'OTHER'


class TestSaveExecutedOrder:
    """Tests for save_executed_order function."""
    
    @patch('common_lib._get_executed_orders_filepath')
    def test_save_new_order(self, mock_filepath):
        """Test saving a new order creates file with correct structure."""
        from common_lib import save_executed_order
        
        # Use a temp directory with a non-existent file
        import tempfile
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, 'test_orders.json')
        mock_filepath.return_value = temp_file
        
        # Arrange
        symbol = "NIFTY24JAN23000CE"
        transaction_type = "BUY"
        price = 125.5
        quantity = 75
        
        # Act
        result = save_executed_order(symbol, transaction_type, price, quantity)
        
        # Assert
        assert result is True
        
        with open(temp_file, 'r') as saved_file:
            data = json.load(saved_file)
            assert 'orders' in data
            assert len(data['orders']) == 1
            order = data['orders'][0]
            assert order['symbol'] == symbol
            assert order['transaction_type'] == transaction_type
            assert order['price'] == price
            assert order['quantity'] == quantity
            assert order['option_type'] == 'CE'
            assert order['expiry'] == '24JAN'
        
        # Cleanup
        os.unlink(temp_file)
        os.rmdir(temp_dir)
    
    @patch('common_lib._get_executed_orders_filepath')
    def test_append_to_existing_orders(self, mock_filepath):
        """Test appending order to existing file."""
        from common_lib import save_executed_order
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            # Pre-populate with existing order
            existing_data = {
                'orders': [{
                    'timestamp': '2026-01-21T10:00:00',
                    'symbol': 'NIFTY24JAN22000PE',
                    'transaction_type': 'SELL',
                    'price': 100.0,
                    'quantity': 75
                }]
            }
            json.dump(existing_data, f)
            f.flush()
            mock_filepath.return_value = f.name
            
            # Arrange
            symbol = "NIFTY24JAN23000CE"
            
            # Act
            result = save_executed_order(symbol, "BUY", 125.5, 75)
            
            # Assert
            assert result is True
            
            with open(f.name, 'r') as saved_file:
                data = json.load(saved_file)
                assert len(data['orders']) == 2
            
            # Cleanup
            os.unlink(f.name)


    @patch('common_lib._get_executed_orders_filepath')
    def test_save_order_stores_order_id_and_algo_source(self, mock_filepath):
        """Test that order_id and algo_source are persisted in the JSON record."""
        from common_lib import save_executed_order

        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, 'test_orders.json')
        mock_filepath.return_value = temp_file

        result = save_executed_order(
            "NIFTY24JAN23000CE", "SELL", 200.0, 75,
            algo_source="Trending_Market_Code",
            order_id="112345678",
        )

        assert result is True
        with open(temp_file) as f:
            data = json.load(f)
        order = data['orders'][0]
        assert order['algo_source'] == "Trending_Market_Code"
        assert order['order_id'] == "112345678"

        os.unlink(temp_file)
        os.rmdir(temp_dir)


class TestLoadTodaysOrders:
    """Tests for load_todays_orders function."""
    
    @patch('common_lib._get_executed_orders_filepath')
    def test_load_orders_sorted_descending(self, mock_filepath):
        """Test orders are returned sorted by timestamp descending."""
        from common_lib import load_todays_orders
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            # Create test data with orders in wrong order
            test_data = {
                'orders': [
                    {'timestamp': '2026-01-21T09:00:00', 'symbol': 'FIRST'},
                    {'timestamp': '2026-01-21T11:00:00', 'symbol': 'THIRD'},
                    {'timestamp': '2026-01-21T10:00:00', 'symbol': 'SECOND'}
                ]
            }
            json.dump(test_data, f)
            f.flush()
            mock_filepath.return_value = f.name
            
            # Act
            orders = load_todays_orders()
            
            # Assert - should be newest first
            assert len(orders) == 3
            assert orders[0]['symbol'] == 'THIRD'
            assert orders[1]['symbol'] == 'SECOND'
            assert orders[2]['symbol'] == 'FIRST'
            
            # Cleanup
            os.unlink(f.name)
    
    @patch('common_lib._get_executed_orders_filepath')
    def test_load_nonexistent_file_returns_empty(self, mock_filepath):
        """Test loading from non-existent file returns empty list."""
        from common_lib import load_todays_orders
        
        mock_filepath.return_value = '/nonexistent/path/orders.json'
        
        # Act
        orders = load_todays_orders()
        
        # Assert
        assert orders == []


class TestCalculateOrderSummary:
    """Tests for calculate_order_summary function."""
    
    @patch('common_lib.load_todays_orders')
    def test_summary_with_matched_orders(self, mock_load):
        """Test P&L calculation for matched buy/sell pairs."""
        from common_lib import calculate_order_summary
        
        # Arrange - 1 buy and 1 sell for same symbol
        mock_load.return_value = [
            {
                'symbol': 'NIFTY24JAN23000CE',
                'option_type': 'CE',
                'expiry': '24JAN',
                'transaction_type': 'BUY',
                'price': 100.0,
                'quantity': 75
            },
            {
                'symbol': 'NIFTY24JAN23000CE',
                'option_type': 'CE',
                'expiry': '24JAN',
                'transaction_type': 'SELL',
                'price': 120.0,
                'quantity': 75
            }
        ]
        
        # Act
        summary = calculate_order_summary()
        
        # Assert
        assert len(summary['ce_summary']) == 1
        assert len(summary['pe_summary']) == 0
        
        ce = summary['ce_summary'][0]
        assert ce['buy_count'] == 75
        assert ce['sell_count'] == 75
        # P&L = (120 - 100) * 75 = 1500
        assert ce['realized_pnl'] == 1500.0
    
    @patch('common_lib.load_todays_orders')
    def test_summary_with_unmatched_orders(self, mock_load):
        """Test P&L is None for unmatched orders (only buy or only sell)."""
        from common_lib import calculate_order_summary
        
        # Arrange - only buy, no sell
        mock_load.return_value = [
            {
                'symbol': 'NIFTY24JAN22000PE',
                'option_type': 'PE',
                'expiry': '24JAN',
                'transaction_type': 'BUY',
                'price': 80.0,
                'quantity': 150
            }
        ]
        
        # Act
        summary = calculate_order_summary()
        
        # Assert
        assert len(summary['pe_summary']) == 1
        pe = summary['pe_summary'][0]
        assert pe['buy_count'] == 150
        assert pe['sell_count'] == 0
        assert pe['realized_pnl'] is None  # NA - no matched pairs
    
    @patch('common_lib.load_todays_orders')
    def test_empty_orders_returns_empty_summaries(self, mock_load):
        """Test empty orders returns empty PE and CE summaries."""
        from common_lib import calculate_order_summary
        
        mock_load.return_value = []
        
        # Act
        summary = calculate_order_summary()
        
        # Assert
        assert summary['ce_summary'] == []
        assert summary['pe_summary'] == []


class TestSaveExecutedOrderAlgoSource:
    """Tests for algo_source parameter in save_executed_order."""
    
    @patch('common_lib._get_executed_orders_filepath')
    @patch('common_lib.tag', 'Trending_Market_Code')
    def test_save_order_with_explicit_algo_source(self, mock_filepath):
        """Test saving order with explicit algo_source parameter."""
        from common_lib import save_executed_order
        
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, 'test_orders.json')
        mock_filepath.return_value = temp_file
        
        # Act - explicit algo_source
        result = save_executed_order(
            "NIFTY24JAN23000CE", "BUY", 125.5, 75,
            algo_source="Custom_Algo"
        )
        
        # Assert
        assert result is True
        with open(temp_file, 'r') as f:
            data = json.load(f)
            assert data['orders'][0]['algo_source'] == 'Custom_Algo'
        
        # Cleanup
        os.unlink(temp_file)
        os.rmdir(temp_dir)
    
    @patch('common_lib._get_executed_orders_filepath')
    @patch('common_lib.tag', 'Trending_Market_Code')
    def test_save_order_uses_global_tag_as_default(self, mock_filepath):
        """Test saving order uses global tag when algo_source not provided."""
        from common_lib import save_executed_order
        
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, 'test_orders.json')
        mock_filepath.return_value = temp_file
        
        # Act - no algo_source provided
        result = save_executed_order("NIFTY24JAN23000CE", "BUY", 125.5, 75)
        
        # Assert
        assert result is True
        with open(temp_file, 'r') as f:
            data = json.load(f)
            assert data['orders'][0]['algo_source'] == 'Trending_Market_Code'
        
        # Cleanup
        os.unlink(temp_file)
        os.rmdir(temp_dir)


class TestLoadSurvivorOrders:
    """Tests for load_survivor_orders function."""
    
    @patch('common_lib.load_todays_orders')
    def test_filters_survivor_orders_only(self, mock_load):
        """Test that only Survivor algo orders are returned."""
        from common_lib import load_survivor_orders
        
        # Arrange - mix of Survivor and non-Survivor orders
        mock_load.return_value = [
            {'symbol': 'NIFTY24JAN23000CE', 'algo_source': 'Trending_Market_Code'},
            {'symbol': 'NIFTY24JAN22000PE', 'algo_source': 'Scraper'},
            {'symbol': 'SENSEX24JAN80000CE', 'algo_source': 'Trend_Mkt_SENSEX'},
            {'symbol': 'BANKNIFTY24JAN50000PE', 'algo_source': 'Unknown'},
        ]
        
        # Act
        orders = load_survivor_orders()
        
        # Assert - only Survivor orders returned
        assert len(orders) == 2
        assert orders[0]['algo_source'] == 'Trending_Market_Code'
        assert orders[1]['algo_source'] == 'Trend_Mkt_SENSEX'
    
    @patch('common_lib.load_todays_orders')
    def test_returns_empty_when_no_survivor_orders(self, mock_load):
        """Test returns empty list when no Survivor orders exist."""
        from common_lib import load_survivor_orders
        
        mock_load.return_value = [
            {'symbol': 'NIFTY24JAN23000CE', 'algo_source': 'Scraper'},
            {'symbol': 'NIFTY24JAN22000PE', 'algo_source': 'Unknown'},
        ]
        
        # Act
        orders = load_survivor_orders()
        
        # Assert
        assert len(orders) == 0
    
    @patch('common_lib.load_todays_orders')
    def test_handles_missing_algo_source(self, mock_load):
        """Test handles orders without algo_source field (legacy orders)."""
        from common_lib import load_survivor_orders
        
        mock_load.return_value = [
            {'symbol': 'NIFTY24JAN23000CE'},  # No algo_source
            {'symbol': 'SENSEX24JAN80000CE', 'algo_source': 'Trend_Mkt_SENSEX'},
        ]
        
        # Act
        orders = load_survivor_orders()
        
        # Assert - only order with matching algo_source returned
        assert len(orders) == 1
        assert orders[0]['algo_source'] == 'Trend_Mkt_SENSEX'


class TestLoadWaveExtractorOrders:
    """Tests for load_wave_extractor_orders function."""

    @patch('common_lib.load_todays_orders')
    def test_filters_wave_extractor_orders_only(self, mock_load):
        """Test that only Wave Extractor algo orders are returned."""
        from common_lib import load_wave_extractor_orders

        # Arrange - mix of Wave Extractor and non-Wave Extractor orders
        mock_load.return_value = [
            {'symbol': 'NIFTY24JAN23000CE', 'algo_source': 'Gap-Odr_Manual'},
            {'symbol': 'NIFTY24JAN22000PE', 'algo_source': 'Trending_Market_Code'},
            {'symbol': 'BANKNIFTY24JAN50000CE', 'algo_source': 'Gap-Odr_Auto'},
            {'symbol': 'SENSEX24JAN80000PE', 'algo_source': 'Trend_Mkt_SENSEX'},
            {'symbol': 'NIFTY24JAN23500CE', 'algo_source': 'Scraper'},
            {'symbol': 'NIFTY24JAN22500PE', 'algo_source': 'Unknown'},
        ]

        # Act
        orders = load_wave_extractor_orders()

        # Assert - only Wave Extractor orders returned (Manual, Auto, Scraper, Unknown)
        assert len(orders) == 4
        assert orders[0]['algo_source'] == 'Gap-Odr_Manual'
        assert orders[1]['algo_source'] == 'Gap-Odr_Auto'
        assert orders[2]['algo_source'] == 'Scraper'
        assert orders[3]['algo_source'] == 'Unknown'

    @patch('common_lib.load_todays_orders')
    def test_returns_empty_when_no_wave_extractor_orders(self, mock_load):
        """Test returns empty list when no Wave Extractor orders exist."""
        from common_lib import load_wave_extractor_orders

        mock_load.return_value = [
            {'symbol': 'NIFTY24JAN23000CE', 'algo_source': 'Trending_Market_Code'},
            {'symbol': 'NIFTY24JAN22000PE', 'algo_source': 'Trend_Mkt_SENSEX'},
        ]

        # Act
        orders = load_wave_extractor_orders()

        # Assert
        assert len(orders) == 0

    @patch('common_lib.load_todays_orders')
    def test_handles_missing_algo_source(self, mock_load):
        """Test handles orders without algo_source field (legacy orders)."""
        from common_lib import load_wave_extractor_orders

        mock_load.return_value = [
            {'symbol': 'NIFTY24JAN23000CE'},  # No algo_source
            {'symbol': 'BANKNIFTY24JAN50000CE', 'algo_source': 'Gap-Odr_Auto'},
        ]

        # Act
        orders = load_wave_extractor_orders()

        # Assert - only order with matching algo_source returned
        assert len(orders) == 1
        assert orders[0]['algo_source'] == 'Gap-Odr_Auto'


class TestCalculateWaveExtractorOrderSummary:
    """Tests for calculate_wave_extractor_order_summary function."""

    @patch('common_lib.load_wave_extractor_orders')
    def test_summary_with_matched_wave_extractor_orders(self, mock_load):
        """Test P&L calculation for matched buy/sell pairs from Wave Extractor."""
        from common_lib import calculate_wave_extractor_order_summary

        # Arrange - matched buy/sell pair
        mock_load.return_value = [
            {
                'symbol': 'NIFTY24JAN23000CE',
                'option_type': 'CE',
                'expiry': '24JAN',
                'transaction_type': 'BUY',
                'price': 100.0,
                'quantity': 75
            },
            {
                'symbol': 'NIFTY24JAN23000CE',
                'option_type': 'CE',
                'expiry': '24JAN',
                'transaction_type': 'SELL',
                'price': 120.0,
                'quantity': 75
            }
        ]

        # Act
        summary = calculate_wave_extractor_order_summary()

        # Assert
        assert len(summary['ce_summary']) == 1
        assert len(summary['pe_summary']) == 0

        ce = summary['ce_summary'][0]
        assert ce['buy_count'] == 75
        assert ce['sell_count'] == 75
        # P&L = (120 - 100) * 75 = 1500
        assert ce['realized_pnl'] == 1500.0

    @patch('common_lib.load_wave_extractor_orders')
    def test_empty_summary_when_no_wave_extractor_orders(self, mock_load):
        """Test returns empty summaries when no Wave Extractor orders."""
        from common_lib import calculate_wave_extractor_order_summary

        mock_load.return_value = []

        # Act
        summary = calculate_wave_extractor_order_summary()

        # Assert
        assert summary['ce_summary'] == []
        assert summary['pe_summary'] == []

    @patch('common_lib.load_wave_extractor_orders')
    def test_summary_excludes_non_wave_extractor_orders(self, mock_load):
        """Test that summary only includes Wave Extractor orders (mock ensures filtering)."""
        from common_lib import calculate_wave_extractor_order_summary

        # Arrange - only Wave Extractor orders (filtering is done by load function)
        mock_load.return_value = [
            {
                'symbol': 'NIFTY24JAN22000PE',
                'option_type': 'PE',
                'expiry': '24JAN',
                'transaction_type': 'SELL',
                'price': 80.0,
                'quantity': 150
            }
        ]

        # Act
        summary = calculate_wave_extractor_order_summary()

        # Assert
        assert len(summary['pe_summary']) == 1
        pe = summary['pe_summary'][0]
        assert pe['sell_count'] == 150
        assert pe['buy_count'] == 0
        assert pe['realized_pnl'] is None  # No matched pairs

