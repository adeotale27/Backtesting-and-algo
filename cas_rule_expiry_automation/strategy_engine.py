"""WebSocket-driven CAS fire strategy.

Subscribes to index ticks. On CAS close publication (ohlc.close flip) — or
optionally on LTP inside the fire window — resolves OTM CE/PE from the
pre-warm cache and market-sells immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Set

from cas_rule_expiry_automation.config import AppConfig
from cas_rule_expiry_automation.expiry_calendar import INDEX_META, today_indexes
from cas_rule_expiry_automation.kite_client import KiteClient
from cas_rule_expiry_automation.order_engine import OrderEngine
from cas_rule_expiry_automation.state import StateStore
from cas_rule_expiry_automation.strike_resolver import StrikeCache
from cas_rule_expiry_automation.time_utils import get_ist_now, in_window

logger = logging.getLogger(__name__)


class StrategyEngine:
    def __init__(
        self,
        client: KiteClient,
        config: AppConfig,
        store: StateStore,
    ) -> None:
        self.client = client
        self.config = config
        self.store = store
        self.cache = StrikeCache()
        self.orders = OrderEngine(
            client,
            lots=config.lots,
            product=config.product,
            live_trading=config.live_trading,
        )
        self._baseline_close: Dict[str, float] = {}
        self._token_to_index: Dict[int, str] = {}
        self._lock = threading.Lock()
        self._firing: Set[str] = set()
        self.active_indexes: List[str] = []

    def setup_for_today(self) -> List[str]:
        indexes = today_indexes(self.config)
        self.active_indexes = indexes
        self._token_to_index = {
            int(INDEX_META[i]["token"]): i for i in indexes
        }
        if not indexes:
            logger.info("Not an expiry day — strategy idle")
        return indexes

    def capture_baselines(self) -> None:
        for index in self.active_indexes:
            key = INDEX_META[index]["spot_key"]
            q = self.client.quote([key])[key]
            prev = float(q.get("ohlc", {}).get("close") or 0)
            ltp = float(q.get("last_price") or 0)
            self._baseline_close[index] = prev
            self.store.set_ltp(index, ltp)
            logger.info("[%s] baseline close=%.2f ltp=%.2f", index, prev, ltp)
            if ltp > 0:
                try:
                    self.cache.prewarm(
                        self.client.kite,
                        index,
                        ltp,
                        self.config.ce_otm_steps,
                        self.config.pe_otm_steps,
                    )
                except Exception as exc:
                    logger.warning("prewarm %s failed: %s", index, exc)

    def on_ticks(self, ticks: List[dict]) -> None:
        if not self.store.is_activated():
            return
        now = get_ist_now()
        in_cas = in_window(now, self.config.watch_start, self.config.watch_end)

        for tick in ticks:
            token = int(tick.get("instrument_token") or 0)
            index = self._token_to_index.get(token)
            if not index:
                continue
            if self.store.has_fired(index):
                continue

            ltp = float(tick.get("last_price") or 0)
            if ltp:
                self.store.set_ltp(index, ltp)

            ohlc = tick.get("ohlc") or {}
            ohlc_close = float(ohlc.get("close") or 0)
            baseline = self._baseline_close.get(index)

            trigger = None
            close_px = None

            if (
                self.config.fire_on_close_update
                and baseline
                and ohlc_close
                and abs(ohlc_close - baseline) > 1e-6
            ):
                # Official close flipped on the wire — fire immediately
                # (even slightly before watch window if Zerodha updates early)
                trigger = "ws_ohlc_close"
                close_px = ohlc_close
            elif (
                self.config.fire_on_ltp_in_window
                and in_cas
                and ltp > 0
            ):
                trigger = "ws_ltp_window"
                close_px = ltp
            elif tick.get("cas_close") and ltp > 0:
                # Backtest replay marker
                trigger = "ws_replay_cas"
                close_px = float((tick.get("ohlc") or {}).get("close") or ltp)

            if trigger and close_px:
                self._fire(index, close_px, trigger)

    def _fire(self, index: str, close_price: float, trigger: str) -> None:
        with self._lock:
            if index in self._firing or self.store.has_fired(index):
                return
            self._firing.add(index)
        t0 = time.perf_counter()
        try:
            legs = self.cache.resolve(
                self.client.kite,
                index,
                close_price,
                self.config.ce_otm_steps,
                self.config.pe_otm_steps,
            )
            fills = self.orders.sell_otm(legs, close_price, trigger, t0)
            self.store.mark_fired(index, close_price, fills)
            logger.info(
                "FIRE complete %s close=%.2f trigger=%s total=%.1fms",
                index,
                close_price,
                trigger,
                (time.perf_counter() - t0) * 1000,
            )
        except Exception as exc:
            logger.exception("Fire failed for %s", index)
            self.store.set_error(f"{index}: {exc}")
            with self._lock:
                self._firing.discard(index)

    def manual_fire(self, index: str, close_price: float) -> list:
        t0 = time.perf_counter()
        legs = self.cache.resolve(
            self.client.kite,
            index,
            close_price,
            self.config.ce_otm_steps,
            self.config.pe_otm_steps,
        )
        fills = self.orders.sell_otm(legs, close_price, "manual", t0)
        self.store.mark_fired(index, close_price, fills)
        return fills
