"""Thin Zerodha Kite Connect session for the CAS Expiry algo.

Intentionally does NOT import ``common_lib`` (which has heavy module-level
side effects). Reuses the vendored ``kiteconnect`` package and optionally
the parent repo's ``instrument_cache`` for token persistence / instrument DB.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

from cas_expiry.config import CasConfig, load_config, save_kite_credentials

logger = logging.getLogger(__name__)

# Allow importing sibling repo modules (instrument_cache) when run as package
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_VENDOR = os.path.join(_REPO_ROOT, "vendor", "pykiteconnect")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


def _kite_connect_cls():
    from kiteconnect import KiteConnect

    return KiteConnect


class KiteSession:
    """Authenticated Kite client wrapper."""

    def __init__(self, config: Optional[CasConfig] = None) -> None:
        self.config = config or load_config()
        self.kite: Any = None

    def connect(self, access_token: Optional[str] = None) -> Any:
        """Build KiteConnect and set access token. Raises on missing creds."""
        api_key = self.config.api_key
        token = (access_token or self.config.access_token or "").strip()

        if not api_key or api_key.startswith("YOUR_"):
            raise RuntimeError(
                "api_key not configured — edit cas_expiry/config.ini or use the admin UI"
            )
        if not token:
            # Fall back to parent dashboard token if available
            token = self._load_parent_token() or ""
        if not token:
            raise RuntimeError(
                "access_token missing — paste today's token in config.ini / admin UI"
            )

        KiteConnect = _kite_connect_cls()
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(token)
        # Persist so restarts keep working the same day
        try:
            save_kite_credentials(
                api_key, self.config.api_secret, token, self.config.config_path
            )
            self._save_parent_token(token)
        except OSError as exc:
            logger.warning("Could not persist access token: %s", exc)
        self.config.access_token = token
        return self.kite

    def login_url(self) -> str:
        if not self.config.api_key or self.config.api_key.startswith("YOUR_"):
            raise RuntimeError("api_key not configured")
        KiteConnect = _kite_connect_cls()
        return KiteConnect(api_key=self.config.api_key).login_url()

    def exchange_request_token(self, request_token: str) -> str:
        """Exchange a request_token for an access_token and persist it."""
        if not self.config.api_secret or self.config.api_secret.startswith("YOUR_"):
            raise RuntimeError("api_secret not configured")
        KiteConnect = _kite_connect_cls()
        kite = KiteConnect(api_key=self.config.api_key)
        data = kite.generate_session(request_token, api_secret=self.config.api_secret)
        access_token = data["access_token"]
        kite.set_access_token(access_token)
        self.kite = kite
        save_kite_credentials(
            self.config.api_key,
            self.config.api_secret,
            access_token,
            self.config.config_path,
        )
        self._save_parent_token(access_token)
        self.config.access_token = access_token
        return access_token

    def profile(self) -> dict:
        if self.kite is None:
            self.connect()
        return self.kite.profile()

    def quote(self, instruments: list[str]) -> dict:
        if self.kite is None:
            self.connect()
        return self.kite.quote(instruments)

    def place_market_sell(
        self,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        product: str,
        tag: str = "CAS",
        live_trading: bool = False,
    ) -> Any:
        """Place a MARKET SELL, or return a synthetic dry-run id."""
        if self.kite is None:
            self.connect()
        if not live_trading:
            logger.warning(
                "[DRY-RUN] SELL %s x%d on %s (%s) — not sent to Zerodha",
                tradingsymbol,
                quantity,
                exchange,
                product,
            )
            return -1
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            product=product,
            order_type=self.kite.ORDER_TYPE_MARKET,
            tag=tag[:20],
            market_protection=getattr(self.kite, "MARKET_PROTECTION_AUTO", -1),
        )

    def sync_instruments(self) -> bool:
        """Refresh instruments.db via parent instrument_cache if available."""
        try:
            import instrument_cache

            if self.kite is None:
                self.connect()
            return bool(instrument_cache.sync_instruments(self.kite))
        except Exception as exc:
            logger.warning("instrument sync skipped: %s", exc)
            return False

    @staticmethod
    def _load_parent_token() -> Optional[str]:
        try:
            import instrument_cache

            return instrument_cache.get_kite_token()
        except Exception:
            return None

    @staticmethod
    def _save_parent_token(token: str) -> None:
        try:
            import instrument_cache

            instrument_cache.save_kite_token(token)
        except Exception:
            pass
