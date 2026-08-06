"""WebSocket-driven CAS fire strategy with detect→sell timing.

Live rule: the moment a KiteTicker tick carries the new CAS close, we sell.
No chart time, no 1-second poll — fire is push-driven on the tick callback.
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
from cas_rule_expiry_automation.timing import new_detect_event

logger = logging.getLogger(__name__)

# IST is UTC+5:30 fixed — used for a cheap window check on every tick.
_IST_OFFSET_SEC = 5 * 3600 + 30 * 60


def _ist_day_seconds() -> float:
    """Seconds since local IST midnight (no datetime alloc)."""
    return (time.time() + _IST_OFFSET_SEC) % 86400.0


def _time_to_day_seconds(t) -> float:
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


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
        self._win_start_sec = _time_to_day_seconds(config.watch_start)
        self._win_end_sec = _time_to_day_seconds(config.watch_end)

    def _refresh_window_bounds(self) -> None:
        self._win_start_sec = _time_to_day_seconds(self.config.watch_start)
        self._win_end_sec = _time_to_day_seconds(self.config.watch_end)

    def setup_for_today(self) -> List[str]:
        indexes = today_indexes(self.config)
        self.active_indexes = indexes
        self._token_to_index = {
            int(INDEX_META[i]["token"]): i for i in indexes
        }
        self._refresh_window_bounds()
        if not indexes:
            logger.info("Not an expiry day — strategy idle")
        return indexes

    def capture_baselines(self) -> None:
        """Load prev-day close into strategy (for CAS detect). Prefer startup pull.

        Does NOT stream LTP — LTP only arrives via WebSocket while CAS is active.
        One quote here is only for strike prewarm spot if needed.
        """
        for index in self.active_indexes:
            # Reuse once-pulled baseline from store when available
            snap = self.store.snapshot()
            cached = (snap.get("baseline_close") or {}).get(index.upper())
            if cached:
                self._baseline_close[index] = float(cached)

            key = INDEX_META[index]["spot_key"]
            q = self.client.quote([key])[key]
            prev = float(q.get("ohlc", {}).get("close") or 0)
            ltp = float(q.get("last_price") or 0)
            if prev:
                self._baseline_close[index] = prev
                self.store.set_baseline(index, prev)
            logger.info("[%s] baseline close=%.2f (spot for prewarm=%.2f)", index, prev, ltp)
            spot = ltp or prev
            if spot > 0:
                try:
                    self.cache.prewarm(
                        self.client.kite,
                        index,
                        spot,
                        self.config.ce_otm_steps,
                        self.config.pe_otm_steps,
                    )
                except Exception as exc:
                    logger.warning("prewarm %s failed: %s", index, exc)

    def on_ticks(self, ticks: List[dict]) -> None:
        """KiteTicker push callback — fire on this tick, do not wait for chart time."""
        if not self.store.is_activated():
            return
        # Cheap IST window check (no datetime objects on the hot path)
        in_cas = self._win_start_sec <= _ist_day_seconds() <= self._win_end_sec
        token_map = self._token_to_index

        for tick in ticks:
            token = int(tick.get("instrument_token") or 0)
            index = token_map.get(token)
            if not index:
                continue
            if index in self._firing or self.store.has_fired(index):
                continue

            ltp = float(tick.get("last_price") or 0)
            if ltp:
                self.store.set_ltp(index, ltp)

            ohlc = tick.get("ohlc") or {}
            ohlc_close = float(ohlc.get("close") or 0)
            baseline = self._baseline_close.get(index)

            trigger = None
            close_px = None

            # Primary live signal: day's ohlc.close flipped from prev-day baseline
            # on this WebSocket tick → start MARKET sells immediately.
            if (
                self.config.fire_on_close_update
                and in_cas
                and baseline
                and ohlc_close
                and abs(ohlc_close - baseline) > 1e-6
            ):
                trigger = "ws_ohlc_close"
                close_px = ohlc_close
            elif self.config.fire_on_ltp_in_window and in_cas and ltp > 0:
                trigger = "ws_ltp_window"
                close_px = ltp
            elif tick.get("cas_close") and ltp > 0:
                trigger = "ws_replay_cas"
                close_px = float((tick.get("ohlc") or {}).get("close") or ltp)

            if trigger and close_px:
                # Claim synchronously so concurrent ticks cannot double-fire,
                # then sell off the ticker thread so the socket stays free.
                with self._lock:
                    if index in self._firing or self.store.has_fired(index):
                        continue
                    self._firing.add(index)
                threading.Thread(
                    target=self._fire_claimed,
                    args=(index, float(close_px), trigger, "live"),
                    name=f"cas-fire-{index}",
                    daemon=True,
                ).start()

    def _fire_claimed(
        self,
        index: str,
        close_price: float,
        trigger: str,
        source: str = "live",
    ) -> list:
        """Fire path after index is already claimed in ``_firing``."""
        self.orders.lots = self.config.lots
        self.orders.product = self.config.product
        self.orders.live_trading = self.config.live_trading

        # Stamp detect the instant we decided to sell (WS tick arrival)
        timing = new_detect_event(index, close_price, trigger, source=source)
        t0 = time.perf_counter()
        logger.info(
            "CAS DETECTED %s close=%.2f at %s trigger=%s → MARKET SELL both legs NOW",
            index,
            close_price,
            timing.cas_detected_at,
            trigger,
        )
        try:
            legs = self.cache.resolve(
                self.client.kite,
                index,
                close_price,
                self.config.ce_otm_steps,
                self.config.pe_otm_steps,
            )
            fills, timing = self.orders.sell_otm(
                legs, close_price, trigger, t0, timing=timing
            )
            self.store.mark_fired(index, close_price, fills, timing=timing)
            logger.info(
                "FIRE done %s detect→done=%sms CE=%sms PE=%sms live=%s",
                index,
                timing.detect_to_done_ms if timing else "?",
                timing.detect_to_ce_ms if timing else "?",
                timing.detect_to_pe_ms if timing else "?",
                self.config.live_trading,
            )
            return fills
        except Exception as exc:
            logger.exception("Fire failed for %s", index)
            self.store.set_error(f"{index}: {exc}")
            with self._lock:
                self._firing.discard(index)
            return []

    def _fire(
        self,
        index: str,
        close_price: float,
        trigger: str,
        source: str = "live",
    ) -> list:
        """CAS detect → cached strikes → parallel MARKET SELL CE+PE."""
        with self._lock:
            if index in self._firing or self.store.has_fired(index):
                return []
            self._firing.add(index)
        return self._fire_claimed(index, close_price, trigger, source=source)

    def manual_fire(self, index: str, close_price: float) -> list:
        return self._fire(index, close_price, "manual", source="manual")
