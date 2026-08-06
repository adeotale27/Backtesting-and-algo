"""KiteTicker WebSocket stream + tick-replay bus for backtests.

Live path uses Zerodha's official WebSocket (lowest practical latency on
public internet). Backtests feed the *same* on_ticks handler with a
synthetic/replayed tick stream so results exercise identical fire logic.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

TickHandler = Callable[[List[dict]], None]


@dataclass
class StreamStats:
    connected: bool = False
    mode: str = "full"
    subscribed: List[int] = field(default_factory=list)
    ticks_received: int = 0
    last_tick_at: Optional[str] = None
    last_tick_age_ms: Optional[float] = None
    inter_tick_ms: Optional[float] = None
    last_error: Optional[str] = None
    source: str = "idle"  # live | replay | idle


class TickBus:
    """Fan-out bus shared by live WebSocket and backtest replay."""

    def __init__(self) -> None:
        self._handlers: List[TickHandler] = []
        self._lock = threading.Lock()
        self.stats = StreamStats()
        self._last_stamp_mono: float = 0.0
        self._last_tick_mono: float = 0.0

    def add_handler(self, handler: TickHandler) -> None:
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def remove_handler(self, handler: TickHandler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def publish(self, ticks: List[dict]) -> None:
        if not ticks:
            return
        # Push path — no sleep, no 1s poll. Stamp gap between ticks for UI.
        n = len(ticks)
        now_mono = time.monotonic()
        if self._last_tick_mono > 0:
            self.stats.inter_tick_ms = round((now_mono - self._last_tick_mono) * 1000.0, 3)
        self._last_tick_mono = now_mono
        self.stats.ticks_received += n
        self.stats.last_tick_age_ms = 0.0
        if now_mono - self._last_stamp_mono >= 0.25:
            self._last_stamp_mono = now_mono
            self.stats.last_tick_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            handlers = list(self._handlers)
        for h in handlers:
            try:
                h(ticks)
            except Exception:
                logger.exception("tick handler failed")


class LiveWebSocket:
    """Zerodha KiteTicker wrapper (threaded)."""

    def __init__(
        self,
        api_key: str,
        access_token: str,
        bus: TickBus,
        mode: str = "full",
    ) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.bus = bus
        self.mode = (mode or "full").lower()
        self._ticker: Any = None
        self._tokens: List[int] = []

    def start(self, tokens: Sequence[int]) -> None:
        from kiteconnect import KiteTicker

        self._tokens = [int(t) for t in tokens]
        self.bus.stats.source = "live"
        self.bus.stats.mode = self.mode
        self.bus.stats.subscribed = list(self._tokens)

        kws = KiteTicker(self.api_key, self.access_token)
        bus = self.bus
        mode = self.mode
        token_list = list(self._tokens)

        def on_ticks(ws, ticks):  # noqa: ANN001
            bus.publish(ticks or [])

        def on_connect(ws, response):  # noqa: ANN001
            bus.stats.connected = True
            logger.info("KiteTicker connected — subscribe %s mode=%s", token_list, mode)
            ws.subscribe(token_list)
            mode_const = {
                "ltp": ws.MODE_LTP,
                "quote": ws.MODE_QUOTE,
                "full": ws.MODE_FULL,
            }.get(mode, ws.MODE_FULL)
            ws.set_mode(mode_const, token_list)

        def on_close(ws, code, reason):  # noqa: ANN001
            bus.stats.connected = False
            logger.warning("KiteTicker closed code=%s reason=%s", code, reason)

        def on_error(ws, code, reason):  # noqa: ANN001
            bus.stats.last_error = f"{code}: {reason}"
            logger.error("KiteTicker error %s %s", code, reason)

        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        kws.on_close = on_close
        kws.on_error = on_error
        self._ticker = kws
        kws.connect(threaded=True)

    def stop(self) -> None:
        try:
            if self._ticker is not None:
                self._ticker.close()
        except Exception:
            pass
        self.bus.stats.connected = False
        self.bus.stats.source = "idle"


class TickReplay:
    """Replays a list of tick dicts through TickBus (backtest WebSocket path)."""

    def __init__(self, bus: TickBus) -> None:
        self.bus = bus

    def run(
        self,
        ticks: Sequence[dict],
        interval_ms: int = 0,
        stop_flag: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Publish ticks one-by-one. Returns count published."""
        self.bus.stats.source = "replay"
        self.bus.stats.connected = True
        n = 0
        delay = max(interval_ms, 0) / 1000.0
        for tick in ticks:
            if stop_flag and stop_flag():
                break
            self.bus.publish([tick])
            n += 1
            if delay:
                time.sleep(delay)
        self.bus.stats.connected = False
        self.bus.stats.source = "idle"
        return n


def candle_to_ticks(
    instrument_token: int,
    candles: Sequence[dict],
    ticks_per_candle: int = 4,
) -> List[dict]:
    """Expand OHLCV candles into a synthetic WebSocket tick stream.

    Path: open → high → low → close (or fewer points). Each tick carries
    ``ohlc`` so the live close-detection path can be exercised.
    """
    out: List[dict] = []
    prev_close: Optional[float] = None
    for c in candles:
        o = float(c["open"])
        h = float(c["high"])
        low = float(c["low"])
        cl = float(c["close"])
        ts = c.get("date")
        path = [o, h, low, cl][: max(1, ticks_per_candle)]
        for px in path:
            ohlc = {
                "open": o,
                "high": h,
                "low": low,
                # During the day Zerodha keeps previous close until CAS print.
                # For replay we keep prev_close until the final candle's close
                # which we treat as the CAS equilibrium.
                "close": prev_close if prev_close is not None else cl,
            }
            out.append(
                {
                    "instrument_token": instrument_token,
                    "last_price": px,
                    "ohlc": ohlc,
                    "timestamp": ts,
                }
            )
        prev_close = cl
    # Final tick: flip ohlc.close to today's close (CAS publication)
    if out and candles:
        last_close = float(candles[-1]["close"])
        out.append(
            {
                "instrument_token": instrument_token,
                "last_price": last_close,
                "ohlc": {
                    "open": float(candles[-1]["open"]),
                    "high": float(candles[-1]["high"]),
                    "low": float(candles[-1]["low"]),
                    "close": last_close,
                },
                "timestamp": candles[-1].get("date"),
                "cas_close": True,
            }
        )
    return out
