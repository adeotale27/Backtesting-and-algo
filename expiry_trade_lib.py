"""
Expiry Trade System Library.

Provides the ExpiryTradeSystem class that:
- Detects today's expiring index (NIFTY or SENSEX)
- Manages a KiteTicker for real-time ticks
- Builds 3-minute OHLC candles from ticks
- Calculates Stochastic RSI using pandas-ta
- Computes 1-hour support/resistance levels
"""

import os
import logging
import datetime
import threading
import time
from common_lib import get_ist_now
from typing import Optional, Dict, List, Any, Tuple
from collections import defaultdict

import pandas as pd
import pandas_ta as ta
from kiteconnect import KiteConnect, KiteTicker


logger = logging.getLogger(__name__)


class ExpiryTradeSystem:
    """
    Core engine for the Expiry Trade dashboard.

    Detects today's expiring index (NIFTY or SENSEX), sets up a
    KiteTicker for real-time spot price ticks, aggregates 3-minute
    candles, and computes Stochastic RSI and support/resistance.

    Attributes:
        kite: Authenticated KiteConnect instance.
        is_active: Whether the ticker is running.
        active_index: 'NIFTY' or 'SENSEX' if expiry today, else None.
        expiry_substring: Common symbol prefix for today's expiry.
    """

    # Spot symbol mapping
    SPOT_SYMBOLS = {
        "NIFTY": "NSE:NIFTY 50",
        "SENSEX": "BSE:SENSEX",
    }

    # Exchange mapping for instrument lookup
    EXCHANGE_MAP = {
        "NIFTY": "NFO",
        "SENSEX": "BFO",
    }

    def __init__(self, kite: KiteConnect) -> None:
        """
        Initialize the Expiry Trade System.

        Args:
            kite: An authenticated KiteConnect client instance.
        """
        self.kite = kite
        self._is_active = False
        self._active_index: Optional[str] = None
        self._expiry_substring: Optional[str] = None
        self._spot_instrument_token: Optional[int] = None
        self._last_tick_time: Optional[datetime.datetime] = None

        # 3-min candle storage
        self._candles: List[Dict[str, Any]] = []
        self._current_candle: Optional[Dict[str, Any]] = None
        self._candle_lock = threading.Lock()

        # 1-hr support/resistance
        self._support_resistance: List[Dict[str, Any]] = []

        # Stochastic RSI cache
        self._stoch_rsi: List[Dict[str, Any]] = []

        # Ticker
        self._kws: Optional[KiteTicker] = None
        self._ticker_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Whether the system ticker is running."""
        return self._is_active

    @property
    def is_stuck(self) -> bool:
        """True if no tick received in the last 10 seconds."""
        if self._last_tick_time is None:
            return True
        return (get_ist_now() - self._last_tick_time).total_seconds() > 10

    @property
    def last_tick_time(self) -> Optional[str]:
        """ISO-formatted timestamp of last tick, or None."""
        if self._last_tick_time:
            return self._last_tick_time.strftime("%H:%M:%S")
        return None

    @property
    def active_index(self) -> Optional[str]:
        """'NIFTY' or 'SENSEX' if expiry today, else None."""
        return self._active_index

    @property
    def expiry_substring(self) -> Optional[str]:
        """Common symbol prefix for today's expiry (e.g. 'SENSEX26219')."""
        return self._expiry_substring

    # ------------------------------------------------------------------
    # Expiry detection
    # ------------------------------------------------------------------

    def detect_expiry(self) -> bool:
        """
        Detect which index (NIFTY or SENSEX) has expiry today.

        Scans instruments from NFO and BFO exchanges. The index whose
        options expire today becomes active. NIFTY is checked first;
        if no NIFTY expiry, SENSEX is checked.

        Returns:
            True if an expiry was found for today, False otherwise.
        """
        today = datetime.date.today()

        for index_name, exchange in [("NIFTY", "NFO"), ("SENSEX", "BFO")]:
            try:
                instruments = self.kite.instruments(exchange)
            except Exception as exc:
                logger.error("Failed to fetch %s instruments: %s", exchange, exc)
                continue

            prefix = index_name
            expiry_symbols = [
                inst["tradingsymbol"]
                for inst in instruments
                if inst["tradingsymbol"].startswith(prefix)
                and inst.get("expiry") == today
                and inst.get("instrument_type") in ("CE", "PE")
            ]

            if expiry_symbols:
                self._active_index = index_name
                self._expiry_substring = self._extract_common_substring(
                    expiry_symbols
                )
                logger.info(
                    "Expiry detected: %s, substring: %s",
                    index_name,
                    self._expiry_substring,
                )
                return True

        logger.info("No NIFTY or SENSEX expiry today.")
        self._active_index = None
        self._expiry_substring = None
        return False

    @staticmethod
    def _extract_common_substring(symbols: List[str]) -> str:
        """
        Find the longest common prefix across a list of trading symbols.

        This dynamically determines the expiry substring (e.g.
        'SENSEX26219') by scanning actual instrument symbols rather
        than using a hardcoded character count.

        Args:
            symbols: List of trading symbols sharing the same expiry.

        Returns:
            The longest common prefix string.
        """
        if not symbols:
            return ""
        prefix = symbols[0]
        for sym in symbols[1:]:
            while not sym.startswith(prefix):
                prefix = prefix[:-1]
                if not prefix:
                    return ""
        # Strip trailing digits that are part of strike price.
        # The common prefix will naturally end right before the
        # strike price diverges (e.g. 'SENSEX26219' is common,
        # then '23000CE' vs '23100PE' differs).
        return prefix

    # ------------------------------------------------------------------
    # Spot token resolution
    # ------------------------------------------------------------------

    def _resolve_spot_token(self) -> Optional[int]:
        """
        Get the instrument token for the active spot index.

        Returns:
            Instrument token integer, or None on failure.
        """
        if not self._active_index:
            return None

        spot_symbol = self.SPOT_SYMBOLS[self._active_index]
        try:
            quote = self.kite.quote(spot_symbol)
            token = quote.get(spot_symbol, {}).get("instrument_token")
            return token
        except Exception as exc:
            logger.error("Failed to resolve spot token for %s: %s", spot_symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Ticker management
    # ------------------------------------------------------------------

    def start(self) -> Dict[str, Any]:
        """
        Start the expiry trade system.

        Detects expiry, resolves spot token, fetches initial candle
        data, and starts the ticker thread.

        Returns:
            Status dict with keys: success, message, active_index,
            expiry_substring.
        """
        if self._is_active:
            return {
                "success": True,
                "message": "System already running",
                "active_index": self._active_index,
                "expiry_substring": self._expiry_substring,
            }

        # Detect expiry
        if not self.detect_expiry():
            return {
                "success": False,
                "message": "No NIFTY or SENSEX expiry today",
                "active_index": None,
                "expiry_substring": None,
            }

        # Resolve spot token
        self._spot_instrument_token = self._resolve_spot_token()
        if not self._spot_instrument_token:
            return {
                "success": False,
                "message": "Could not resolve spot instrument token",
                "active_index": self._active_index,
                "expiry_substring": self._expiry_substring,
            }

        # Fetch initial candle data
        self._fetch_initial_candles()
        self._fetch_support_resistance()
        self._recalculate_stoch_rsi()

        # Start ticker
        self._start_ticker()

        return {
            "success": True,
            "message": f"System started for {self._active_index}",
            "active_index": self._active_index,
            "expiry_substring": self._expiry_substring,
        }

    def stop(self) -> None:
        """Stop the ticker and mark system inactive."""
        self._is_active = False
        if self._kws:
            try:
                self._kws.close()
            except Exception:
                pass
        logger.info("Expiry Trade System stopped.")

    def _start_ticker(self) -> None:
        """Set up and start KiteTicker in a background thread."""
        api_key = self.kite.api_key
        access_token = self.kite.access_token

        self._kws = KiteTicker(api_key, access_token)
        self._kws.on_ticks = self._on_ticks
        self._kws.on_connect = self._on_connect
        self._kws.on_close = self._on_close
        self._kws.on_error = self._on_error

        self._is_active = True
        self._kws.connect(threaded=True)
        logger.info("KiteTicker started for token %s", self._spot_instrument_token)

    def _on_connect(self, ws: Any, response: Any) -> None:
        """Subscribe to spot index on successful connect."""
        token = self._spot_instrument_token
        if token:
            ws.subscribe([token])
            ws.set_mode(ws.MODE_FULL, [token])
            logger.info("Subscribed to token %s", token)

    def _on_ticks(self, ws: Any, ticks: List[Dict]) -> None:
        """Process incoming ticks — update last tick time and candle."""
        self._last_tick_time = get_ist_now()
        for tick in ticks:
            if tick.get("instrument_token") == self._spot_instrument_token:
                ltp = tick.get("last_price")
                if ltp is not None:
                    self._update_candle(ltp)

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        """Handle ticker close."""
        logger.warning("KiteTicker closed: code=%s reason=%s", code, reason)

    def _on_error(self, ws: Any, code: Any, reason: Any) -> None:
        """Handle ticker error."""
        logger.error("KiteTicker error: code=%s reason=%s", code, reason)

    # ------------------------------------------------------------------
    # 3-Minute candle aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _get_candle_bucket(dt: datetime.datetime) -> datetime.datetime:
        """
        Get the start timestamp for the 3-minute bucket containing dt.

        Args:
            dt: Datetime to bucket.

        Returns:
            Datetime rounded down to nearest 3-minute boundary.
        """
        minute = dt.minute - (dt.minute % 3)
        return dt.replace(minute=minute, second=0, microsecond=0)

    def _update_candle(self, price: float) -> None:
        """
        Update the current 3-minute candle with a new tick price.

        When the bucket changes, the previous candle is finalized and
        appended to the candles list, and the Stochastic RSI is
        recalculated.

        Args:
            price: Latest tick price.
        """
        now = get_ist_now()
        bucket = self._get_candle_bucket(now)

        with self._candle_lock:
            if self._current_candle is None or self._current_candle["time"] != bucket:
                # Save previous candle
                if self._current_candle is not None:
                    self._candles.append(self._current_candle.copy())
                    # Recalculate indicators after each new completed candle
                    self._recalculate_stoch_rsi()

                # Start new candle
                self._current_candle = {
                    "time": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                }
            else:
                # Update existing candle
                self._current_candle["high"] = max(
                    self._current_candle["high"], price
                )
                self._current_candle["low"] = min(
                    self._current_candle["low"], price
                )
                self._current_candle["close"] = price

    def _fetch_initial_candles(self) -> None:
        """
        Fetch 3-minute historical candles from Kite API.

        Fetches 5 days of history (including today) so that RSI(14)
        and Stochastic(14) have enough warmup data for accurate
        Stochastic RSI calculation. The last candle is popped into
        _current_candle if it falls in the current 3-min bucket,
        preventing duplicate timestamps when ticks arrive.
        """
        if not self._spot_instrument_token:
            return

        today = datetime.date.today()
        # Fetch 5 days of 3-min candles for RSI warmup
        from_date = today - datetime.timedelta(days=5)
        try:
            data = self.kite.historical_data(
                instrument_token=self._spot_instrument_token,
                from_date=from_date,
                to_date=today,
                interval="3minute",
            )
            self._candles = []
            for candle in data:
                candle_time = (
                    candle["date"].replace(tzinfo=None)
                    if hasattr(candle["date"], "replace")
                    else candle["date"]
                )
                self._candles.append(
                    {
                        "time": candle_time,
                        "open": candle["open"],
                        "high": candle["high"],
                        "low": candle["low"],
                        "close": candle["close"],
                    }
                )

            # Seed _current_candle from the last historical candle
            # if it belongs to the current 3-min bucket, to avoid
            # duplicate timestamps when live ticks arrive.
            if self._candles:
                now = get_ist_now()
                current_bucket = self._get_candle_bucket(now)
                last_candle = self._candles[-1]
                last_bucket = self._get_candle_bucket(last_candle["time"])
                if last_bucket == current_bucket:
                    self._current_candle = self._candles.pop()

            logger.info(
                "Fetched %d initial 3-min candles (5-day history).",
                len(self._candles),
            )
        except Exception as exc:
            logger.error("Failed to fetch initial candles: %s", exc)
            self._candles = []

    # ------------------------------------------------------------------
    # Support / Resistance (1-hour candles)
    # ------------------------------------------------------------------

    def _fetch_support_resistance(self) -> None:
        """
        Fetch today's 1-hour candles and extract support/resistance.

        Support = candle low, Resistance = candle high for each
        completed 1-hour candle.
        """
        if not self._spot_instrument_token:
            return

        today = datetime.date.today()
        try:
            data = self.kite.historical_data(
                instrument_token=self._spot_instrument_token,
                from_date=today,
                to_date=today,
                interval="60minute",
            )
            self._support_resistance = []
            for candle in data:
                self._support_resistance.append(
                    {
                        "time": candle["date"].replace(tzinfo=None).strftime(
                            "%H:%M"
                        )
                        if hasattr(candle["date"], "strftime")
                        else str(candle["date"]),
                        "support": candle["low"],
                        "resistance": candle["high"],
                    }
                )
            logger.info(
                "Fetched %d 1-hr candles for S/R.", len(self._support_resistance)
            )
        except Exception as exc:
            logger.error("Failed to fetch S/R data: %s", exc)
            self._support_resistance = []

    # ------------------------------------------------------------------
    # Stochastic RSI (via pandas-ta)
    # ------------------------------------------------------------------

    def _recalculate_stoch_rsi(self) -> None:
        """
        Recalculate Stochastic RSI from all available 3-min candles.

        Uses pandas-ta with parameters:
        - RSI length: 14
        - Stochastic length: 14
        - K smoothing: 3
        - D smoothing: 3

        Calculation uses the full candle history (including prior
        days) for accurate RSI warmup, but only today's values are
        included in the output.

        Results stored in self._stoch_rsi as list of
        {time, k, d} dicts.
        """
        if len(self._candles) < 35:
            # Need RSI(14) + Stoch(14) + K(3) + D(3) warmup
            self._stoch_rsi = []
            return

        try:
            closes = pd.Series([c["close"] for c in self._candles])
            result = ta.stochrsi(
                close=closes,
                length=14,
                rsi_length=14,
                k=3,
                d=3,
            )

            if result is None or result.empty:
                self._stoch_rsi = []
                return

            k_col = [c for c in result.columns if "K" in c.upper()][0]
            d_col = [c for c in result.columns if "D" in c.upper()][0]

            today = datetime.date.today()
            self._stoch_rsi = []
            for i, row in result.iterrows():
                k_val = row[k_col]
                d_val = row[d_col]
                if pd.notna(k_val) and pd.notna(d_val):
                    candle_time = self._candles[i]["time"]
                    # Only include today's data in the output
                    candle_date = (
                        candle_time.date()
                        if hasattr(candle_time, "date")
                        else today
                    )
                    if candle_date != today:
                        continue
                    time_str = (
                        candle_time.strftime("%H:%M")
                        if hasattr(candle_time, "strftime")
                        else str(candle_time)
                    )
                    self._stoch_rsi.append(
                        {
                            "time": time_str,
                            "k": round(float(k_val), 2),
                            "d": round(float(d_val), 2),
                        }
                    )
        except Exception as exc:
            logger.error("Stochastic RSI calculation failed: %s", exc)
            self._stoch_rsi = []

    # ------------------------------------------------------------------
    # Data getters (for API endpoints)
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """
        Get current system status.

        Returns:
            Dict with is_active, is_stuck, active_index,
            expiry_substring, last_tick_time.
        """
        return {
            "is_active": self._is_active,
            "is_stuck": self.is_stuck if self._is_active else False,
            "active_index": self._active_index,
            "expiry_substring": self._expiry_substring,
            "last_tick_time": self.last_tick_time,
        }

    def get_candles(self) -> List[Dict[str, Any]]:
        """
        Get today's 3-minute candles including the current one.

        Filters to today only (multi-day history is kept for
        indicator warmup), deduplicates by timestamp, and sorts
        to ensure strictly increasing order for LightweightCharts.

        Returns:
            List of candle dicts with time (ISO str), open, high,
            low, close.
        """
        today = datetime.date.today()

        with self._candle_lock:
            all_candles = list(self._candles)
            if self._current_candle:
                all_candles.append(self._current_candle.copy())

        # Filter to today only and deduplicate by timestamp
        seen_times = set()
        result = []
        for c in all_candles:
            candle_time = c["time"]
            # Filter to today's candles only
            candle_date = (
                candle_time.date()
                if hasattr(candle_time, "date")
                else today
            )
            if candle_date != today:
                continue

            time_str = (
                candle_time.strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(candle_time, "strftime")
                else str(candle_time)
            )
            # Skip duplicates (keep last seen for latest close)
            if time_str in seen_times:
                # Replace existing entry with latest data
                result = [r for r in result if r["time"] != time_str]
            seen_times.add(time_str)
            result.append(
                {
                    "time": time_str,
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                }
            )

        # Sort by time to ensure strictly increasing order
        result.sort(key=lambda x: x["time"])
        return result

    def get_stoch_rsi(self) -> List[Dict[str, Any]]:
        """
        Get the latest Stochastic RSI values.

        Returns:
            List of {time, k, d} dicts.
        """
        return list(self._stoch_rsi)

    def get_support_resistance(self) -> List[Dict[str, Any]]:
        """
        Get today's 1-hour support/resistance levels.

        Returns:
            List of {time, support, resistance} dicts.
        """
        # Re-fetch to include any new completed 1-hr candles
        self._fetch_support_resistance()
        return list(self._support_resistance)
