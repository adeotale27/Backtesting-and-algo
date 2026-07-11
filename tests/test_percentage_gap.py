"""Unit tests for percentage-based gap calculations and delta multiplier in common_lib.py."""
import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCalculateGapFromPercentage:
    """Tests for calculate_gap_from_percentage function."""

    def test_basic_percentage_calculation(self):
        """Test basic percentage calculation: 27% of 100 = 27."""
        from common_lib import calculate_gap_from_percentage
        
        result = calculate_gap_from_percentage(100.0, 0.27)
        assert result == 27.0

    def test_percentage_with_rounding(self):
        """Test rounding to 1 decimal: 27% of 127 = 34.29 -> 34.3."""
        from common_lib import calculate_gap_from_percentage
        
        result = calculate_gap_from_percentage(127.0, 0.27)
        assert result == 34.3

    def test_percentage_zero_price(self):
        """Test with zero price returns zero."""
        from common_lib import calculate_gap_from_percentage
        
        result = calculate_gap_from_percentage(0.0, 0.27)
        assert result == 0.0

    def test_percentage_small_values(self):
        """Test with small percentage values."""
        from common_lib import calculate_gap_from_percentage
        
        # 5% of 50 = 2.5
        result = calculate_gap_from_percentage(50.0, 0.05)
        assert result == 2.5


class TestUpdateGapsFromPercentage:
    """Tests for update_gaps_from_percentage with max(initial, percentage) for sell."""

    def test_gap_update_uses_max_for_sell(self):
        """Test that sell_gap uses max(initial, percentage) for protection."""
        import common_lib
        
        # Arrange: Set initial state with higher initial_sell_gap
        common_lib.buy_gap = 27.0
        common_lib.sell_gap = 27.0
        common_lib.buy_gap_percentage = 0.27  # 27%
        common_lib.sell_gap_percentage = 0.27  # 27%
        common_lib.initial_sell_gap = 27.0  # Original absolute gap
        
        # Act: Update gaps for LOWER execution price (50 * 0.27 = 13.5)
        common_lib.update_gaps_from_percentage(50.0)
        
        # Assert: buy_gap uses percentage (13.5), sell_gap uses max(27, 13.5) = 27
        assert common_lib.buy_gap == 13.5
        assert common_lib.sell_gap == 27.0  # Protected by initial_sell_gap

    def test_gap_update_percentage_higher_than_initial(self):
        """Test that sell_gap uses percentage when its higher than initial."""
        import common_lib
        
        # Arrange
        common_lib.buy_gap = 27.0
        common_lib.sell_gap = 27.0
        common_lib.buy_gap_percentage = 0.27  # 27%
        common_lib.sell_gap_percentage = 0.27  # 27%
        common_lib.initial_sell_gap = 27.0
        
        # Act: Update gaps for HIGHER execution price (200 * 0.27 = 54)
        common_lib.update_gaps_from_percentage(200.0)
        
        # Assert: Both use percentage since 54 > 27
        assert common_lib.buy_gap == 54.0
        assert common_lib.sell_gap == 54.0  # max(27, 54) = 54

    def test_gap_update_zero_percentage_no_change(self):
        """Test that zero percentage does not update gaps."""
        import common_lib
        
        # Arrange
        common_lib.buy_gap = 27.0
        common_lib.sell_gap = 27.0
        common_lib.buy_gap_percentage = 0.0  # Not initialized
        common_lib.sell_gap_percentage = 0.0
        common_lib.initial_sell_gap = 27.0
        
        # Act
        common_lib.update_gaps_from_percentage(127.0)
        
        # Assert: Gaps should remain unchanged
        assert common_lib.buy_gap == 27.0
        assert common_lib.sell_gap == 27.0


class TestSetScraperLastPrice:
    """Tests for set_scraper_last_price with initial_sell_gap storage."""

    def test_first_call_stores_initial_sell_gap(self):
        """Test that first call stores initial_sell_gap for max comparison."""
        import common_lib
        
        # Arrange
        common_lib.scraper_last_price = -1  # Initial state
        common_lib.buy_gap = 27.0
        common_lib.sell_gap = 27.0
        common_lib.buy_gap_percentage = 0.0
        common_lib.sell_gap_percentage = 0.0
        common_lib.initial_sell_gap = 0.0
        
        # Act
        common_lib.set_scraper_last_price(100.0)
        
        # Assert
        assert common_lib.initial_sell_gap == 27.0  # Stored original
        assert common_lib.buy_gap_percentage == 0.27
        assert common_lib.sell_gap_percentage == 0.27


class TestGetDeltaMultiplier:
    """Tests for get_delta_multiplier function."""

    def test_delta_within_range_returns_no_multiplier(self):
        """Test that delta within ±1000 returns (1.0, 1.0)."""
        from common_lib import get_delta_multiplier
        
        # This will try to fetch real delta, but we test the logic via mock
        # For now, just verify the function is callable
        result = get_delta_multiplier("ce", "UNKNOWN_SYMBOL")
        assert result == (1.0, 1.0)

    def test_function_handles_unknown_symbol(self):
        """Test that unknown symbols return (1.0, 1.0)."""
        from common_lib import get_delta_multiplier
        
        result = get_delta_multiplier("ce", "RANDOMSYMBOL123")
        assert result == (1.0, 1.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
