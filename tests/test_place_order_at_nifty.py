"""Tests for place_order_at_nifty.log_nifty_status throttling.

place_order_at_nifty does ``from common_lib import *`` at import time, which
would initialise a live KiteConnect session. To keep the import side-effect
free, the module is imported inside a module-scoped fixture with ``kiteconnect``
and ``common_lib`` temporarily replaced by mocks via ``patch.dict(sys.modules)``,
which restores the real modules afterwards.

IMPORTANT: never assign to ``sys.modules`` at module level in this file — a
previous version did that during pytest collection and poisoned ``common_lib``
for every test file imported after it (~65 unrelated failures).
"""

import datetime
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="module")
def nifty_module():
    """Import place_order_at_nifty with mocked kiteconnect/common_lib.

    Yields:
        The imported place_order_at_nifty module with get_ist_now stubbed.
    """
    mock_common_lib = MagicMock()
    mock_common_lib.get_ist_now.return_value = datetime.datetime(2026, 4, 6, 14, 30, 0)

    with patch.dict(
        sys.modules,
        {"kiteconnect": MagicMock(), "common_lib": mock_common_lib},
    ):
        # Force a fresh import so module-level code binds against the mocks.
        sys.modules.pop("place_order_at_nifty", None)
        module = importlib.import_module("place_order_at_nifty")
        # `from common_lib import *` on a MagicMock imports nothing usable —
        # inject the stub the tests rely on directly.
        module.get_ist_now = mock_common_lib.get_ist_now
        yield module

    # Drop the mock-built module so any later import gets a clean copy.
    sys.modules.pop("place_order_at_nifty", None)


@pytest.fixture(autouse=True)
def reset_counter(nifty_module):
    """Reset the global log counter before each test."""
    nifty_module.nifty_control_log_counter = 0


def test_log_nifty_status_throttling(nifty_module):
    """Test that log_nifty_status only logs every 10th call."""
    with patch("logging.error") as mock_log_error:
        symbol = "NIFTY26407"
        pe_val = 22925.3
        ce_val = 22640.0
        curr_val = 22923.0
        gap = 18.0

        # Call 1 to 9 - should not log
        for _ in range(1, 10):
            nifty_module.log_nifty_status(symbol, pe_val, ce_val, curr_val, gap)
            assert mock_log_error.call_count == 0

        # Call 10 - should log
        nifty_module.log_nifty_status(symbol, pe_val, ce_val, curr_val, gap)
        assert mock_log_error.call_count == 1

        # Verify log message content
        args, _ = mock_log_error.call_args
        assert "Nifty still under control" in args[0]
        assert "pe_value = 22925.3" in args[0]
        assert "Checking with CE gap of 18.0" in args[0]

        # Call 11 to 19 - should not log additional times
        for _ in range(11, 20):
            nifty_module.log_nifty_status(symbol, pe_val, ce_val, curr_val, gap)
            assert mock_log_error.call_count == 1

        # Call 20 - should log again
        nifty_module.log_nifty_status(symbol, pe_val, ce_val, curr_val, gap)
        assert mock_log_error.call_count == 2
