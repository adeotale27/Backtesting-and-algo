"""CAS Expiry strategy — wire detector → strikes → executor."""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from cas_expiry.cas_detector import CasCloseDetector, CloseSignal
from cas_expiry.config import CasConfig
from cas_expiry.executor import OrderExecutor
from cas_expiry.kite_session import KiteSession
from cas_expiry.state import FillRecord, StateStore
from cas_expiry.strikes import detect_expiry_prefix, has_expiry_today, resolve_legs

logger = logging.getLogger(__name__)


class CasExpiryStrategy:
    """Core strategy controller for one or more indexes."""

    def __init__(
        self,
        session: KiteSession,
        config: CasConfig,
        store: StateStore,
    ) -> None:
        self.session = session
        self.config = config
        self.store = store
        self.executor = OrderExecutor(
            session=session,
            lots=config.lots,
            product=config.product,
            live_trading=config.live_trading,
            tag="CAS",
        )

    def fire_on_signal(self, signal: CloseSignal) -> List[FillRecord]:
        """Resolve ATM±N legs from the close and market-sell them immediately."""
        logger.info(
            "Firing CAS strategy for %s close=%.2f source=%s",
            signal.index,
            signal.close_price,
            signal.source,
        )
        prefix = detect_expiry_prefix(self.session.kite, signal.index)
        legs = resolve_legs(
            kite=self.session.kite,
            index=signal.index,
            close_price=signal.close_price,
            ce_offset=self.config.ce_offset,
            pe_offset=self.config.pe_offset,
            expiry_prefix=prefix,
        )
        fills = self.executor.sell_legs(legs, signal.close_price)
        self.store.mark_fired(signal.close_price, fills, index=signal.index)
        return fills

    def run_index(
        self,
        index: str,
        stop_flag: Optional[Callable[[], bool]] = None,
        force_signal: Optional[CloseSignal] = None,
    ) -> Optional[List[FillRecord]]:
        """Watch CAS window for one index and fire once close arrives."""
        if self.config.require_expiry_today:
            if not has_expiry_today(self.session.kite, index):
                msg = f"{index}: no options expiry today — skipping"
                logger.info(msg)
                return None

        detector = CasCloseDetector(
            kite=self.session.kite,
            index=index,
            watch_start=self.config.watch_start,
            watch_end=self.config.watch_end,
            poll_interval_seconds=self.config.poll_interval_seconds,
        )
        detector.capture_baseline()

        if force_signal is not None:
            signal = force_signal
        else:
            signal = detector.wait_for_close(
                stop_flag=stop_flag,
                on_tick=lambda _t: self.store.heartbeat(),
            )
        if signal is None:
            return None
        return self.fire_on_signal(signal)

    def manual_fire(self, index: str, close_price: float) -> List[FillRecord]:
        """Admin-triggered fire with an explicit close price (tests / recovery)."""
        detector = CasCloseDetector(
            kite=self.session.kite,
            index=index,
            watch_start=self.config.watch_start,
            watch_end=self.config.watch_end,
            poll_interval_seconds=self.config.poll_interval_seconds,
        )
        signal = detector.manual(close_price)
        return self.fire_on_signal(signal)

    def force_ltp_fire(self, index: str) -> List[FillRecord]:
        detector = CasCloseDetector(
            kite=self.session.kite,
            index=index,
            watch_start=self.config.watch_start,
            watch_end=self.config.watch_end,
            poll_interval_seconds=self.config.poll_interval_seconds,
        )
        signal = detector.force_from_ltp()
        return self.fire_on_signal(signal)
