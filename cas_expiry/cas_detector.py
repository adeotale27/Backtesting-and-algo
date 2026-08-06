"""Low-latency detector for the SEBI Closing Auction Session close price.

During Order Entry Session II the exchange randomly ends order entry between
15:28 and 15:30 IST, then matches and publishes the equilibrium closing price.
This module polls Zerodha quotes at a configurable millisecond interval and
signals as soon as today's close is observed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import time as dtime
from typing import Any, Callable, Dict, Optional

from cas_expiry.strikes import INDEX_META
from cas_expiry.time_utils import get_ist_now, in_window

logger = logging.getLogger(__name__)


@dataclass
class CloseSignal:
    """Emitted when the CAS closing price is detected (or forced)."""

    index: str
    close_price: float
    last_price: float
    previous_close: float
    detected_at: str
    source: str  # "ohlc_close" | "manual" | "forced_ltp"
    latency_ms: float = 0.0
    raw_quote: Optional[Dict[str, Any]] = None


class CasCloseDetector:
    """Polls index quotes until ``ohlc.close`` flips to today's close."""

    def __init__(
        self,
        kite: Any,
        index: str,
        watch_start: dtime,
        watch_end: dtime,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if index not in INDEX_META:
            raise ValueError(f"Unsupported index: {index}")
        self.kite = kite
        self.index = index
        self.spot_key = INDEX_META[index]["spot_key"]
        self.watch_start = watch_start
        self.watch_end = watch_end
        self.poll_interval_seconds = max(poll_interval_seconds, 0.001)
        self.previous_close: Optional[float] = None
        self._armed_at: Optional[float] = None

    def capture_baseline(self) -> float:
        """Record yesterday's close from the current quote (call before window)."""
        q = self._fetch_quote()
        prev = float(q.get("ohlc", {}).get("close") or 0.0)
        self.previous_close = prev
        logger.info(
            "[%s] Baseline previous close=%.2f last=%.2f",
            self.index,
            prev,
            float(q.get("last_price") or 0.0),
        )
        return prev

    def in_cas_window(self, now=None) -> bool:
        now = now or get_ist_now()
        return in_window(now, self.watch_start, self.watch_end)

    def check_once(self) -> Optional[CloseSignal]:
        """Single quote poll. Returns CloseSignal if today's close is visible."""
        t0 = time.perf_counter()
        q = self._fetch_quote()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        last = float(q.get("last_price") or 0.0)
        ohlc_close = float(q.get("ohlc", {}).get("close") or 0.0)

        if self.previous_close is None:
            self.previous_close = ohlc_close

        # Primary signal: ohlc.close changed away from the morning baseline.
        # Zerodha updates ohlc.close to the official exchange close once published.
        if (
            self.previous_close
            and ohlc_close > 0
            and abs(ohlc_close - self.previous_close) > 1e-6
        ):
            return CloseSignal(
                index=self.index,
                close_price=ohlc_close,
                last_price=last,
                previous_close=self.previous_close,
                detected_at=get_ist_now().isoformat(),
                source="ohlc_close",
                latency_ms=latency_ms,
                raw_quote=q,
            )
        return None

    def wait_for_close(
        self,
        stop_flag: Optional[Callable[[], bool]] = None,
        on_tick: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[CloseSignal]:
        """Busy-poll inside the CAS window until close is detected or window ends."""
        if self.previous_close is None:
            self.capture_baseline()

        self._armed_at = time.perf_counter()
        logger.info(
            "[%s] Armed for CAS window %s–%s (poll=%.0fms)",
            self.index,
            self.watch_start.isoformat(timespec="seconds"),
            self.watch_end.isoformat(timespec="seconds"),
            self.poll_interval_seconds * 1000,
        )

        while True:
            if stop_flag and stop_flag():
                logger.info("[%s] Detector stopped by flag", self.index)
                return None

            now = get_ist_now()
            if not self.in_cas_window(now):
                # Before window: wait; after window: give up
                if now.timetz().replace(tzinfo=None) > self.watch_end:
                    logger.warning(
                        "[%s] CAS window ended without close detection", self.index
                    )
                    return None
                time.sleep(min(0.25, self.poll_interval_seconds * 5))
                continue

            try:
                signal = self.check_once()
            except Exception as exc:
                logger.warning("[%s] quote poll error: %s", self.index, exc)
                time.sleep(self.poll_interval_seconds)
                continue

            if on_tick:
                try:
                    on_tick({"index": self.index, "ts": now.isoformat()})
                except Exception:
                    pass

            if signal is not None:
                elapsed = (time.perf_counter() - (self._armed_at or t0_safe())) * 1000
                logger.info(
                    "[%s] CLOSE DETECTED source=%s price=%.2f quote_latency=%.1fms armed_for=%.0fms",
                    self.index,
                    signal.source,
                    signal.close_price,
                    signal.latency_ms,
                    elapsed,
                )
                return signal

            time.sleep(self.poll_interval_seconds)

    def force_from_ltp(self) -> CloseSignal:
        """Emergency / test path: treat current LTP as the close."""
        q = self._fetch_quote()
        last = float(q.get("last_price") or 0.0)
        prev = float(q.get("ohlc", {}).get("close") or self.previous_close or 0.0)
        return CloseSignal(
            index=self.index,
            close_price=last,
            last_price=last,
            previous_close=prev,
            detected_at=get_ist_now().isoformat(),
            source="forced_ltp",
            raw_quote=q,
        )

    def manual(self, close_price: float) -> CloseSignal:
        """Admin-supplied close price (paper / recovery)."""
        q: Dict[str, Any] = {}
        try:
            q = self._fetch_quote()
        except Exception:
            pass
        last = float(q.get("last_price") or close_price)
        prev = float(
            (q.get("ohlc") or {}).get("close")
            or self.previous_close
            or close_price
        )
        return CloseSignal(
            index=self.index,
            close_price=float(close_price),
            last_price=last,
            previous_close=prev,
            detected_at=get_ist_now().isoformat(),
            source="manual",
            raw_quote=q or None,
        )

    def _fetch_quote(self) -> Dict[str, Any]:
        data = self.kite.quote([self.spot_key])
        if self.spot_key not in data:
            raise RuntimeError(f"No quote returned for {self.spot_key}")
        return data[self.spot_key]


def t0_safe() -> float:
    return time.perf_counter()
