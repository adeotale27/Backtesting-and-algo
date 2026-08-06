"""Thin Kite Connect REST client (orders / instruments / auth)."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Optional

from cas_rule_expiry_automation.config import AppConfig, load_config, save_kite_credentials

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR = os.path.join(_ROOT, "vendor", "pykiteconnect")
for p in (_ROOT, _VENDOR):
    if p not in sys.path:
        sys.path.insert(0, p)


def _KiteConnect():
    from kiteconnect import KiteConnect

    return KiteConnect


class KiteClient:
    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or load_config()
        self.kite: Any = None

    def connect(self, access_token: Optional[str] = None) -> Any:
        key = self.config.api_key
        token = (access_token or self.config.access_token or "").strip()
        if not key or key.startswith("YOUR_"):
            raise RuntimeError("api_key not set — use config.ini or the UI")
        if not token:
            token = self._parent_token() or ""
        if not token:
            raise RuntimeError("access_token missing")
        KC = _KiteConnect()
        self.kite = KC(api_key=key)
        self.kite.set_access_token(token)
        save_kite_credentials(key, self.config.api_secret, token, self.config.config_path)
        self._save_parent_token(token)
        self.config.access_token = token
        return self.kite

    def login_url(self) -> str:
        return _KiteConnect()(api_key=self.config.api_key).login_url()

    def exchange_request_token(self, request_token: str) -> str:
        KC = _KiteConnect()
        kite = KC(api_key=self.config.api_key)
        data = kite.generate_session(request_token, api_secret=self.config.api_secret)
        token = data["access_token"]
        kite.set_access_token(token)
        self.kite = kite
        save_kite_credentials(
            self.config.api_key, self.config.api_secret, token, self.config.config_path
        )
        self._save_parent_token(token)
        self.config.access_token = token
        return token

    def profile(self) -> dict:
        if not self.kite:
            self.connect()
        return self.kite.profile()

    def quote(self, keys: list[str]) -> dict:
        if not self.kite:
            self.connect()
        return self.kite.quote(keys)

    def instruments(self, exchange: str) -> list:
        if not self.kite:
            self.connect()
        return self.kite.instruments(exchange)

    def historical(self, token: int, start, end, interval: str = "minute") -> list:
        if not self.kite:
            self.connect()
        return self.kite.historical_data(token, start, end, interval)

    def place_market_sell(
        self,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        product: str,
        tag: str = "CASRULE",
        live: bool = False,
    ) -> Any:
        """Punch a MARKET SELL immediately (no limit price, no quote round-trip)."""
        if not self.kite:
            self.connect()
        if not live:
            logger.warning(
                "[DRY-RUN] MARKET SELL %s x%d %s/%s",
                tradingsymbol,
                quantity,
                exchange,
                product,
            )
            return f"DRY-{tradingsymbol}-{int(time.time()*1000)%100000}"

        # Explicit MARKET params — never LIMIT / SL / price.
        params = dict(
            variety=self.kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=int(quantity),
            product=product,
            order_type=self.kite.ORDER_TYPE_MARKET,
            validity=self.kite.VALIDITY_DAY,
            price=0,
            trigger_price=0,
            tag=(tag or "CASRULE")[:20],
        )
        # BFO/NFO market protection when SDK supports it
        mp = getattr(self.kite, "MARKET_PROTECTION_AUTO", None)
        if mp is not None:
            params["market_protection"] = mp
        logger.info(
            "Kite place_order MARKET SELL %s/%s x%d product=%s",
            exchange,
            tradingsymbol,
            quantity,
            product,
        )
        return self.kite.place_order(**params)

    @staticmethod
    def _parent_token() -> Optional[str]:
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
