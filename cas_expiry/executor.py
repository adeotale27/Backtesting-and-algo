"""Order executor — market-sell ATM±N CE/PE legs with dry-run safety."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from cas_expiry.kite_session import KiteSession
from cas_expiry.state import FillRecord
from cas_expiry.strikes import StrikeLeg, get_lot_size
from cas_expiry.time_utils import get_ist_now

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Places MARKET SELL orders for resolved CAS legs."""

    def __init__(
        self,
        session: KiteSession,
        lots: int = 1,
        product: str = "NRML",
        live_trading: bool = False,
        tag: str = "CAS",
    ) -> None:
        self.session = session
        self.lots = max(int(lots), 1)
        self.product = product
        self.live_trading = bool(live_trading)
        self.tag = tag[:20]

    def sell_legs(self, legs: List[StrikeLeg], close_price: float) -> List[FillRecord]:
        """Sell every leg at market. Returns fill records (dry-run or live)."""
        fills: List[FillRecord] = []
        for leg in legs:
            lot = leg.lot_size or get_lot_size(leg.index)
            qty = self.lots * lot
            price = self._ltp(leg)
            try:
                order_id = self.session.place_market_sell(
                    exchange=leg.exchange,
                    tradingsymbol=leg.tradingsymbol,
                    quantity=qty,
                    product=self.product,
                    tag=self.tag,
                    live_trading=self.live_trading,
                )
            except Exception as exc:
                logger.exception("Order failed for %s: %s", leg.tradingsymbol, exc)
                fills.append(
                    FillRecord(
                        ts=get_ist_now().isoformat(),
                        index=leg.index,
                        leg=leg.leg,
                        tradingsymbol=leg.tradingsymbol,
                        strike=leg.strike,
                        side="SELL",
                        quantity=qty,
                        order_id=None,
                        price=price,
                        dry_run=not self.live_trading,
                        note=f"ERROR: {exc}",
                    )
                )
                continue

            fills.append(
                FillRecord(
                    ts=get_ist_now().isoformat(),
                    index=leg.index,
                    leg=leg.leg,
                    tradingsymbol=leg.tradingsymbol,
                    strike=leg.strike,
                    side="SELL",
                    quantity=qty,
                    order_id=order_id,
                    price=price,
                    dry_run=not self.live_trading,
                    note=f"close={close_price}",
                )
            )
            logger.info(
                "SELL %s %s x%d @~%.2f order_id=%s dry_run=%s",
                leg.leg,
                leg.tradingsymbol,
                qty,
                price,
                order_id,
                not self.live_trading,
            )
        return fills

    def _ltp(self, leg: StrikeLeg) -> float:
        key = f"{leg.exchange}:{leg.tradingsymbol}"
        try:
            q = self.session.quote([key])
            return float(q[key]["last_price"])
        except Exception:
            return 0.0
