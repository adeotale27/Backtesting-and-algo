"""Process orchestration — activate → WebSocket → strategy."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from cas_rule_expiry_automation.config import AppConfig, load_config
from cas_rule_expiry_automation.expiry_calendar import INDEX_META, describe_today
from cas_rule_expiry_automation.kite_client import KiteClient
from cas_rule_expiry_automation.state import StateStore, get_store
from cas_rule_expiry_automation.strategy_engine import StrategyEngine
from cas_rule_expiry_automation.time_utils import get_ist_now, in_window, time_only
from cas_rule_expiry_automation.ws_stream import LiveWebSocket, TickBus

logger = logging.getLogger(__name__)


class AutomationEngine:
    """Background controller for CAS Rule Expiry Automation."""

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        store: Optional[StateStore] = None,
    ) -> None:
        self.config = config or load_config()
        self.store = store or get_store()
        self.bus = TickBus()
        self.client: Optional[KiteClient] = None
        self.strategy: Optional[StrategyEngine] = None
        self.ws: Optional[LiveWebSocket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws_started = False
        self._indexes_day: Optional[str] = None
        self._indexes_cache: list[str] = []
        self._last_ws_status_at: float = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cas-rule-engine", daemon=True
        )
        self._thread.start()
        logger.info("Automation engine started")

    def stop(self) -> None:
        self._stop.set()
        self._stop_ws()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def reload_config(self) -> None:
        self.config = load_config(self.config.config_path)
        # Paper↔live or calendar knobs may change which indexes to watch
        self._indexes_day = None
        self._indexes_cache = []
        if self.strategy:
            self.strategy.config = self.config
            self.strategy.orders.lots = self.config.lots
            self.strategy.orders.product = self.config.product
            self.strategy.orders.live_trading = self.config.live_trading

    def status(self) -> dict:
        day = describe_today(self.config)
        return {
            "runner_alive": self.running,
            "ws": self.bus.stats.__dict__,
            "day": day,
            "state": self.store.snapshot(),
            "config": {
                "lots": self.config.lots,
                "ce_otm_steps": self.config.ce_otm_steps,
                "pe_otm_steps": self.config.pe_otm_steps,
                "product": self.config.product,
                "live_trading": self.config.live_trading,
                "paper_any_day": self.config.paper_any_day,
                "has_token": bool((self.config.access_token or "").strip()),
                "has_key": bool(
                    (self.config.api_key or "").strip()
                    and not self.config.api_key.upper().startswith("YOUR_")
                ),
                "has_secret": bool(
                    (self.config.api_secret or "").strip()
                    and not self.config.api_secret.upper().startswith("YOUR_")
                ),
                "watch_start": self.config.watch_start.isoformat(timespec="seconds"),
                "watch_end": self.config.watch_end.isoformat(timespec="seconds"),
                "ws_mode": self.config.ws_mode,
                "fire_on_close_update": self.config.fire_on_close_update,
                "fire_on_ltp_in_window": self.config.fire_on_ltp_in_window,
            },
        }

    def _ensure_strategy(self) -> StrategyEngine:
        if self.strategy is None:
            self.config = load_config(self.config.config_path)
            self.client = KiteClient(self.config)
            self.client.connect()
            self.strategy = StrategyEngine(self.client, self.config, self.store)
            self.bus.add_handler(self.strategy.on_ticks)
        return self.strategy

    def _start_ws(self, indexes: list[str]) -> None:
        if self._ws_started or not indexes:
            return
        assert self.client is not None
        tokens = [int(INDEX_META[i]["token"]) for i in indexes]
        self.ws = LiveWebSocket(
            api_key=self.config.api_key,
            access_token=self.config.access_token,
            bus=self.bus,
            mode=self.config.ws_mode,
        )
        self.ws.start(tokens)
        self._ws_started = True
        logger.info("Live WebSocket started for %s", indexes)

    def _stop_ws(self) -> None:
        if self.ws:
            self.ws.stop()
            self.ws = None
        self._ws_started = False

    def _today_indexes(self, strategy: StrategyEngine) -> list[str]:
        day = get_ist_now().date().isoformat()
        if self._indexes_day != day:
            self._indexes_cache = strategy.setup_for_today()
            self._indexes_day = day
        return self._indexes_cache

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Throttle WS heartbeat into state (UI only) — never on every spin.
                now_mono = time.monotonic()
                if now_mono - self._last_ws_status_at >= 0.5:
                    self._last_ws_status_at = now_mono
                    self.store.set_ws(
                        self.bus.stats.connected, self.bus.stats.ticks_received
                    )

                if not self.store.is_activated():
                    if self._ws_started:
                        self._stop_ws()
                    time.sleep(1.0)
                    continue

                strategy = self._ensure_strategy()
                indexes = self._today_indexes(strategy)
                if not indexes:
                    time.sleep(2.0)
                    continue

                now = get_ist_now()
                # Pre-warm + WS connect ahead of the CAS window
                prewarm_start = _shift(self.config.watch_start, -self.config.prewarm_minutes)
                tnow = time_only(now)
                if tnow >= prewarm_start:
                    if not strategy.cache.ready_for:
                        try:
                            strategy.capture_baselines()
                        except Exception as exc:
                            self.store.set_error(str(exc))
                    if not self._ws_started:
                        self._start_ws(indexes)

                # Engine loop only manages connect/prewarm — fire is push-based
                # on KiteTicker. Stay responsive in-window without burning CPU.
                if in_window(now, self.config.watch_start, self.config.watch_end):
                    time.sleep(0.01)
                elif tnow >= prewarm_start:
                    time.sleep(0.1)
                else:
                    time.sleep(0.5)
            except Exception as exc:
                logger.exception("engine loop: %s", exc)
                self.store.set_error(str(exc))
                time.sleep(2)


def _shift(t, minutes: int):
    from datetime import datetime, timedelta

    base = datetime(2000, 1, 1, t.hour, t.minute, t.second)
    return (base + timedelta(minutes=minutes)).time()


_ENGINE: Optional[AutomationEngine] = None
_ELOCK = threading.Lock()


def get_engine() -> AutomationEngine:
    global _ENGINE
    with _ELOCK:
        if _ENGINE is None:
            _ENGINE = AutomationEngine()
        return _ENGINE
