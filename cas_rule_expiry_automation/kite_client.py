"""Thin Kite Connect REST client (orders / instruments / auth)."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Optional

from cas_rule_expiry_automation.config import AppConfig, load_config, save_kite_credentials
from cas_rule_expiry_automation.deps import ensure_kite_deps

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR = os.path.join(_ROOT, "vendor", "pykiteconnect")
for p in (_ROOT, _VENDOR):
    if p not in sys.path:
        sys.path.insert(0, p)


def _KiteConnect():
    ensure_kite_deps()
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

    def previous_session_close(self, instrument_token: int, asof=None) -> float:
        """Official previous trading-day close from daily history.

        Do NOT use quote ``ohlc.close`` overnight — Kite keeps that as the
        prior session until ~06:30 IST BOD, so after hours it is one day stale.
        Historical daily bars already carry the latest completed session close.
        """
        from datetime import date, datetime, timedelta

        from cas_rule_expiry_automation.time_utils import get_ist_now

        if asof is None:
            asof = get_ist_now().date()
        elif isinstance(asof, datetime):
            asof = asof.date()
        elif not isinstance(asof, date):
            asof = date.fromisoformat(str(asof)[:10])

        start = asof - timedelta(days=15)
        bars = self.historical(int(instrument_token), start, asof, "day") or []

        def _bar_day(bar) -> Optional[date]:
            dt = bar.get("date")
            if dt is None:
                return None
            if isinstance(dt, datetime):
                try:
                    return dt.astimezone().date() if dt.tzinfo else dt.date()
                except Exception:
                    return dt.date()
            if isinstance(dt, date):
                return dt
            try:
                return date.fromisoformat(str(dt)[:10])
            except Exception:
                return None

        # Prefer last bar strictly before ``asof`` (previous session).
        prior = []
        for b in bars:
            d = _bar_day(b)
            if d is not None and d < asof:
                prior.append(b)
        if prior:
            return float(prior[-1]["close"])
        if bars:
            return float(bars[-1]["close"])
        return 0.0

    def place_market_sell(
        self,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        product: str,
        tag: str = "CASRULE",
        live: bool = False,
    ) -> Any:
        """Punch a MARKET SELL per Kite Connect v3 rules.

        Official place_order for MARKET:
          - order_type=MARKET
          - do NOT send price (LIMIT-only) or trigger_price (SL/SL-M only)
          - market_protection is required (-1 = auto); protection=0 is rejected

        Matches Kite Connect MARKET place_order (no price/trigger_price).
        SDK strips None kwargs before POST.
        """
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

        # Never pass price=0 / trigger_price=0 — those are LIMIT/SL fields and
        # the pykiteconnect client omits only None (0 would be sent to Kite).
        protection = getattr(self.kite, "MARKET_PROTECTION_AUTO", -1)
        logger.info(
            "Kite place_order MARKET SELL %s/%s x%d product=%s protection=%s",
            exchange,
            tradingsymbol,
            quantity,
            product,
            protection,
        )
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=int(quantity),
            product=product,
            order_type=self.kite.ORDER_TYPE_MARKET,
            tag=(tag or "CASRULE")[:20],
            market_protection=protection,
            # price / trigger_price intentionally omitted (None → stripped by SDK)
        )

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
