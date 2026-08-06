"""CAS Expiry Algo — SEBI Closing Auction Session premium capture.

Standalone Zerodha algo that, when admin-activated, watches the CAS
closing-price window (≈15:28–15:30 IST) and immediately market-sells
ATM+1 Call and ATM-1 Put once the official close is published.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
