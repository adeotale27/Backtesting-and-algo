"""Delta-preserving anchor rebalancing logic for Survivor algo scripts.

After each PE sell the market has shifted; this module finds the new spot level
where the full portfolio (existing positions + newly sold options) produces the
same delta as it did when the CE anchor was originally configured.  Symmetric
logic applies after CE sells (find new PE anchor).

Uses greeks_lib (pluggable Black-Scholes backend) and instrument_cache for strike lookup.
"""

from __future__ import annotations

import datetime
import logging

import greeks_lib as mibian

import instrument_cache


def compute_expiry_delta_at_spot(
    positions_net: list[dict],
    symbol_initials: str,
    hypothetical_spot: float,
    volatility: float,
    interest_rate: float,
) -> float:
    """Sum delta of all positions matching symbol_initials at a hypothetical spot.

    Evaluates Black-Scholes delta for each matching position using
    `hypothetical_spot` as the underlying price (NOT the live price), so callers
    can query "what would my portfolio delta be if the market were at X?".

    Args:
        positions_net: kite.positions()['net'] list.
        symbol_initials: Prefix filter e.g. "NIFTY26JUN".  Only positions whose
            tradingsymbol starts with this string are included.
        hypothetical_spot: Underlying price to evaluate delta at.
        volatility: Annual implied volatility in percent (e.g. 15 for 15%).
        interest_rate: Annual risk-free rate in percent (e.g. 6.5).

    Returns:
        Total net delta summed across all matching positions.  Returns 0.0 if
        no matching positions exist.
    """
    total_delta = 0.0
    today = datetime.date.today()

    for position in positions_net:
        tradingsymbol: str = position.get("tradingsymbol", "")
        if not tradingsymbol.startswith(symbol_initials):
            continue
        quantity: int = position.get("quantity", 0)
        if quantity == 0:
            continue

        instrument = instrument_cache.get_instrument(tradingsymbol)
        if instrument is None:
            logging.warning(
                f"Delta rebalance: instrument not found for {tradingsymbol} — skipping"
            )
            continue

        expiry_date = datetime.date.fromisoformat(str(instrument["expiry"]))
        dte = max((expiry_date - today).days, 0.0001)

        try:
            bs = mibian.BS(
                [hypothetical_spot, float(instrument["strike"]), interest_rate, dte],
                volatility=volatility,
            )
            if instrument["instrument_type"] == "CE":
                total_delta += bs.callDelta * quantity
            elif instrument["instrument_type"] == "PE":
                total_delta += bs.putDelta * quantity
        except Exception as exc:
            logging.warning(
                f"Delta rebalance: mibian error for {tradingsymbol}: {exc}"
            )

    return total_delta


def find_spot_for_target_delta(
    positions_net: list[dict],
    symbol_initials: str,
    target_delta: float,
    search_center: float,
    volatility: float,
    interest_rate: float,
    search_half_width: float = 3000.0,
    tolerance: float = 1.0,
) -> float | None:
    """Binary search for the spot where portfolio delta equals target_delta.

    For short strangle portfolios, net delta is monotonically decreasing with
    spot (short calls add negative delta, which grows as spot rises), so binary
    search converges reliably.

    Args:
        positions_net: kite.positions()['net'] list.
        symbol_initials: Prefix filter, e.g. "NIFTY26JUN".
        target_delta: Delta value to locate.
        search_center: Mid-point of the search range (use current live spot).
        volatility: Annual IV % from configfile.ini [option_details].
        interest_rate: Risk-free rate % from configfile.ini [option_details].
        search_half_width: Half the search range in points.  Use 3000 for NIFTY
            (~24000), 8000 for SENSEX (~80000).
        tolerance: Stop when |computed_delta − target_delta| < tolerance.

    Returns:
        Spot price (float) where delta ≈ target_delta, or None if target falls
        outside the search range.
    """
    low = search_center - search_half_width
    high = search_center + search_half_width

    d_low = compute_expiry_delta_at_spot(
        positions_net, symbol_initials, low, volatility, interest_rate
    )
    d_high = compute_expiry_delta_at_spot(
        positions_net, symbol_initials, high, volatility, interest_rate
    )

    if not (min(d_low, d_high) <= target_delta <= max(d_low, d_high)):
        logging.warning(
            f"Delta rebalance: target {target_delta:.2f} outside search range "
            f"[{min(d_low, d_high):.2f}, {max(d_low, d_high):.2f}] "
            f"center={search_center:.0f} ±{search_half_width:.0f}"
        )
        return None

    for _ in range(60):
        mid = (low + high) / 2.0
        d_mid = compute_expiry_delta_at_spot(
            positions_net, symbol_initials, mid, volatility, interest_rate
        )
        if abs(d_mid - target_delta) < tolerance:
            return mid
        # Delta decreases with spot → if mid delta > target, search higher spot
        if d_mid > target_delta:
            low = mid
        else:
            high = mid

    return (low + high) / 2.0
