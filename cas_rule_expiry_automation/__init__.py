"""CAS Rule Expiry Automation — SEBI Closing Auction Session algo.

WebSocket-first Zerodha automation for weekly expiry days:
  • Tuesday  → NIFTY
  • Thursday → SENSEX

Fires market sells on ATM±N (configurable OTM) the instant the index
price / close updates on KiteTicker.
"""

__version__ = "1.0.0"
__title__ = "CAS Rule Expiry Automation"
