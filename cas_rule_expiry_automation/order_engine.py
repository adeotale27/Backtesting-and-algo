"""Ultra-fast market-sell path with per-leg submission timestamps."""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from cas_rule_expiry_automation.kite_client import KiteClient
from cas_rule_expiry_automation.state import Fill
from cas_rule_expiry_automation.strike_resolver import Leg
from cas_rule_expiry_automation.time_utils import get_ist_now
from cas_rule_expiry_automation.timing import TimingEvent, ms_between

logger = logging.getLogger(__name__)


def _iso_ms() -> str:
    return get_ist_now().isoformat(timespec="milliseconds")


class OrderEngine:
    def __init__(
        self,
        client: KiteClient,
        lots: int = 1,
        product: str = "NRML",
        live_trading: bool = False,
    ) -> None:
        self.client = client
        self.lots = max(int(lots), 1)
        self.product = product
        self.live_trading = bool(live_trading)

    def sell_otm(
        self,
        legs: List[Leg],
        close_price: float,
        trigger: str,
        fire_started_at: float,
        timing: Optional[TimingEvent] = None,
    ) -> Tuple[List[Fill], Optional[TimingEvent]]:
        fills: List[Fill] = []
        for leg in legs:
            qty = self.lots * max(int(leg.lot_size), 1)
            t_leg = time.perf_counter()
            submitted_at = _iso_ms()
            price = 0.0
            order_id: object = None
            err = None
            try:
                order_id = self.client.place_market_sell(
                    exchange=leg.exchange,
                    tradingsymbol=leg.tradingsymbol,
                    quantity=qty,
                    product=self.product,
                    tag="CASRULE",
                    live=self.live_trading,
                )
            except Exception as exc:
                logger.exception("SELL failed %s", leg.tradingsymbol)
                err = exc
                order_id = None

            ack_at = _iso_ms()
            latency = (time.perf_counter() - fire_started_at) * 1000
            leg_ms = (time.perf_counter() - t_leg) * 1000
            cas_at = timing.cas_detected_at if timing else None
            detect_to_leg = ms_between(cas_at, ack_at) if cas_at else latency

            if timing is not None:
                if leg.opt_type == "CE":
                    timing.ce_sold_at = ack_at
                    timing.ce_symbol = leg.tradingsymbol
                    timing.ce_order_id = order_id
                    timing.detect_to_ce_ms = round(detect_to_leg, 3)
                elif leg.opt_type == "PE":
                    timing.pe_sold_at = ack_at
                    timing.pe_symbol = leg.tradingsymbol
                    timing.pe_order_id = order_id
                    timing.detect_to_pe_ms = round(detect_to_leg, 3)

            fill = Fill(
                ts=ack_at,
                index=leg.index,
                opt_type=leg.opt_type,
                tradingsymbol=leg.tradingsymbol,
                strike=leg.strike,
                quantity=qty,
                order_id=order_id,
                price=price,
                dry_run=not self.live_trading,
                trigger=f"{trigger}|ERR:{err}" if err else trigger,
                close_price=close_price,
                latency_ms=round(latency, 3),
                cas_detected_at=cas_at,
                order_submitted_at=submitted_at,
            )
            fills.append(fill)
            logger.info(
                "SELL %s %s x%d order=%s detect→sell=%.1fms leg=%.1fms dry=%s",
                leg.opt_type,
                leg.tradingsymbol,
                qty,
                order_id,
                detect_to_leg,
                leg_ms,
                not self.live_trading,
            )

        if timing is not None:
            timing.dry_run = not self.live_trading
            done = timing.pe_sold_at or timing.ce_sold_at
            if done and timing.cas_detected_at:
                timing.detect_to_done_ms = round(
                    ms_between(timing.cas_detected_at, done), 3
                )

        return fills, timing
