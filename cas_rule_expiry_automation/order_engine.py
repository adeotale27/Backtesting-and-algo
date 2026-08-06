"""Ultra-fast market-sell path — payloads pre-built where possible."""

from __future__ import annotations

import logging
import time
from typing import List

from cas_rule_expiry_automation.kite_client import KiteClient
from cas_rule_expiry_automation.state import Fill
from cas_rule_expiry_automation.strike_resolver import Leg
from cas_rule_expiry_automation.time_utils import get_ist_now

logger = logging.getLogger(__name__)


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
    ) -> List[Fill]:
        fills: List[Fill] = []
        for leg in legs:
            qty = self.lots * max(int(leg.lot_size), 1)
            t_leg = time.perf_counter()
            price = 0.0
            try:
                # Skip quote when racing the clock — price is informational only
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
                fills.append(
                    Fill(
                        ts=get_ist_now().isoformat(),
                        index=leg.index,
                        opt_type=leg.opt_type,
                        tradingsymbol=leg.tradingsymbol,
                        strike=leg.strike,
                        quantity=qty,
                        order_id=None,
                        price=price,
                        dry_run=not self.live_trading,
                        trigger=trigger,
                        close_price=close_price,
                        latency_ms=(time.perf_counter() - fire_started_at) * 1000,
                        # note via unused — keep Fill clean; encode in trigger
                    )
                )
                # attach error on trigger string
                fills[-1].trigger = f"{trigger}|ERR:{exc}"
                continue

            latency = (time.perf_counter() - fire_started_at) * 1000
            leg_ms = (time.perf_counter() - t_leg) * 1000
            logger.info(
                "SELL %s %s x%d order=%s total_latency=%.1fms leg=%.1fms dry=%s",
                leg.opt_type,
                leg.tradingsymbol,
                qty,
                order_id,
                latency,
                leg_ms,
                not self.live_trading,
            )
            fills.append(
                Fill(
                    ts=get_ist_now().isoformat(),
                    index=leg.index,
                    opt_type=leg.opt_type,
                    tradingsymbol=leg.tradingsymbol,
                    strike=leg.strike,
                    quantity=qty,
                    order_id=order_id,
                    price=price,
                    dry_run=not self.live_trading,
                    trigger=trigger,
                    close_price=close_price,
                    latency_ms=latency,
                )
            )
        return fills
