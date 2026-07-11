import sys
import os
import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_app import app

# Tests exercise handlers directly — CSRF tokens and rate limits are
# browser-facing protections and would only add noise here.
app.config["WTF_CSRF_ENABLED"] = False
app.config["RATELIMIT_ENABLED"] = False

# Order-path tests exercise clamping/retry logic against a mocked kite —
# the dry-run gate would short-circuit them before the logic under test.
import common_lib
common_lib.live_trading_enabled = True

@pytest.fixture
def client():
    """Create a Flask test client with an authenticated session."""
    app.config['TESTING'] = True
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['app_authenticated'] = True
        yield client

@pytest.fixture
def unauthenticated_client():
    """Create a Flask test client without authentication."""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client
