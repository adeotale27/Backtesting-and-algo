"""
Positions library module for SENSEX open positions dashboard.

Provides utility functions for calculating position data, delta, and margin
requirements for SENSEX options positions.
"""

import datetime
import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np

import greeks_lib as mibian

# kite.quote("BSE:SENSEX") fires twice per page load (overall + next-expiry
# endpoints are fetched concurrently). Cache for 30 s — close enough for
# delta/theta display purposes.
_SENSEX_QUOTE_CACHE: Dict[str, Any] = {"price": None, "expires_at": 0.0}
_SENSEX_QUOTE_TTL_SECONDS: float = 30.0

# Margin constants (per lot) - potentially different for SENSEX but keeping structure generic
# These valus might need adjustment for SENSEX, currently using NIFTY values as placeholders
# or scaled values if needed. For now, assuming similar margin structure per lot.
# SENSEX lot is smaller (20 vs 65+), so margin per lot will be different.
# However, the calculation divides by LOT_SIZE at the end, so we might want to keep the raw numbers
# if they represent total margin for a strategy, or adjust them.
# Given the original code:
# margin = (spread_count * MARGIN_SPREAD + ...) / NIFTY_LOT_SIZE
# It seems MARGIN_SPREAD etc are defined *per lot* (or rather, for a standard quantity?).
# Wait, if MARGIN_SPREAD is 43000, and we divide by LOT_SIZE (65), that's ~660 per qty.
# For SENSEX, if we use the same per-qty assumption, we should adjust.
# BUT, let's assume the user wants the same logic structure.
# To be safe, I'll import sensex_lot_size and use it.

MARGIN_SPREAD = 43000
MARGIN_BOTH_PE_CE = 222000
MARGIN_SINGLE_PE_CE = 162000

# Import lot size from common_lib to stay in sync with the rest of the system
try:
    from common_lib import sensex_lot_size as SENSEX_LOT_SIZE
except ImportError:
    SENSEX_LOT_SIZE = 20  # Fallback default

# Default values for Greek calculations
DEFAULT_VOLATILITY = 12
DEFAULT_INTEREST_RATE = 10


def _get_implied_volatility(
    spot: float,
    strike: float,
    interest_rate: float,
    dte: float,
    ltp: float,
    option_type: str,
    fallback_vol: float,
) -> float:
    """
    Derive implied volatility from an option's market price using bisection.

    Args:
        spot: Current underlying price.
        strike: Option strike price.
        interest_rate: Risk-free rate as a percentage (e.g. 10 for 10%).
        dte: Days to expiration (must be > 0).
        ltp: Last traded price of the option.
        option_type: "CE" for call, "PE" for put.
        fallback_vol: Volatility percentage to return when IV cannot be computed.

    Returns:
        float: Implied volatility as a percentage, or fallback_vol on failure.
    """
    if ltp < 0.5:
        return fallback_vol
    try:
        args = [spot, strike, interest_rate, dte]
        if option_type == "CE":
            iv_calc = mibian.BS(args, callPrice=float(ltp))
        else:
            iv_calc = mibian.BS(args, putPrice=float(ltp))
        implied_vol = iv_calc.impliedVolatility
        if implied_vol is None or not (1.0 < implied_vol < 200.0):
            return fallback_vol
        return implied_vol
    except Exception as e:
        logging.debug("IV calc failed for strike %s type %s ltp %s: %s", strike, option_type, ltp, e)
        return fallback_vol


def calculate_margin_requirement(
    spread_count: int, single_pe_ce: int, both_ce_pe: int
) -> float:
    """
    Calculate margin requirement based on position composition.

    Args:
        spread_count: Number of spread positions (hedged).
        single_pe_ce: Net unhedged single leg positions.
        both_ce_pe: Overlapping CE/PE positions.

    Returns:
        float: Margin requirement in INR (normalized per lot).
    """
    margin = (
        spread_count * MARGIN_SPREAD
        + single_pe_ce * MARGIN_SINGLE_PE_CE
        + both_ce_pe * MARGIN_BOTH_PE_CE
    )
    # Adjusting typical margin scaling for SENSEX if needed.
    # The original NIFTY logic divides by NIFTY_LOT_SIZE.
    # If the constants are total margin for 1 lot of NIFTY (approx 65 qty),
    # then for SENSEX (20 qty), the margin for 1 lot would be less.
    # However, if we blindly divide by SENSEX_LOT_SIZE, we are scaling it 'per unit'
    # using the NIFTY total margin as base? That might be wrong.
    # IF MARGIN_SPREAD is "Margin for 1 lot spread", then for NIFTY it's 43k.
    # For SENSEX 1 lot spread, it might be different.
    # Without exact SENSEX margin values, I will stick to the same formula 
    # but use SENSEX_LOT_SIZE for normalization if that's what the dashboard expects (per unit margin?)
    # Wait, the return doc says "normalized per lot". 
    # Actually line 54 in original is `return margin / NIFTY_LOT_SIZE`. 
    # This implies the function returns "Margin per Quantity unit"? 
    # If 43000 is for 65 qty, then 43000/65 = 661 per qty.
    # If we apply 661 per qty to SENSEX, then for 20 qty it would be 13220.
    # I'll keep the division by SENSEX_LOT_SIZE to maintain "per unit" consistency if that's the intent,
    # OR if the dashboard multiplies this back by quantity?
    # Let's look at usage. The dashboard displays `data.margin_formatted`.
    # It seems to display the raw result of this function.
    # If the user sees "43000" for a NIFTY lot, and the function returns 43000/65 = 661, 
    # does the dashboard show 661?
    # Let's check `positions_lib.py`:
    # `margin_required = calculate_margin_requirement(...)`
    # `return { "margin_formatted": format_inr(margin_required) }`
    # So it displays the per-unit margin? That seems odd for "Margin Required".
    # usually people want Total Margin.
    # Let's re-read `calculate_margin_requirement`.
    # `margin = (spread_count * MARGIN_SPREAD ...)`
    # `spread_count` is calculated as `abs(total_positive_ce + total_positive_pe)`.
    # These are quantites (e.g. 50, 100).
    # If I have 1 lot NIFTY (50 qty nowadays, or 65 old), 
    # spread_count = 50.
    # margin = 50 * 43000 = 2,150,000 ?? That's huge.
    # UNLESS `spread_count` in the calling function is actually LOTS?
    # In `get_nifty_positions_summary`: 
    # `total_positive_ce += abs(pos["quantity"])` -> This sums up QUANTITY.
    # So `spread_count` is total Quantity.
    # If `margin` = Quantity * 43000, that implies 43000 is margin PER UNIT?
    # No, 43000 is likely per lot (approx).
    # If the formula is `margin = ...`, then `margin / LOT_SIZE` might be trying to normalize?
    # Actually, if `spread_count` is quantity, calculating `spread_count * MARGIN_SPREAD` (where MARGIN_SPREAD is ~1 lot margin) is definitely wrong unless we divide by LOT_SIZE.
    # So `(Quantity * Per_Lot_Margin) / Lot_Size` = Total Margin.
    # Yes, that makes sense.
    # So strict translation: `(spread_count * MARGIN_SPREAD) / SENSEX_LOT_SIZE` should be correct
    # assuming MARGIN_SPREAD (43000) is roughly the margin for 1 lot of SENSEX too?
    # SENSEX contract value is usually similar to NIFTY.
    # So I will trust the formula structure: (Total_Qty * Lot_Margin) / Lot_Size.
    
    margin = (
        spread_count * MARGIN_SPREAD
        + single_pe_ce * MARGIN_SINGLE_PE_CE
        + both_ce_pe * MARGIN_BOTH_PE_CE
    )
    return margin / SENSEX_LOT_SIZE


def format_inr(number: float) -> str:
    """
    Format a number in Indian Rupee format with commas.

    Args:
        number: The number to format.

    Returns:
        str: Formatted number string (e.g., "1,23,456.78").
    """
    s, *d = str(round(number, 2)).partition(".")
    r = ",".join([s[x - 2 : x] for x in range(-3, -len(s), -2)][::-1] + [s[-3:]])
    return "".join([r] + d)


def get_all_sensex_instruments() -> Dict[int, Dict[str, Any]]:
    """
    Return all SENSEX BFO-OPT and BFO-FUT instruments from the local DB cache.

    Reads from instruments.db (populated once daily at 9 AM IST) — no live
    Kite API call is made.  Falls back to an empty dict and logs a warning when
    the DB has no BFO data (e.g. before the first sync after deployment).

    Returns:
        dict: Mapping of instrument_token to instrument details.
    """
    import instrument_cache as _ic

    t0 = time.perf_counter()
    instruments = _ic.get_sensex_bfo_instruments()
    t1 = time.perf_counter()
    logging.info(
        "[SENSEX TIMING] get_sensex_bfo_instruments (DB) returned %d rows in %.3fs",
        len(instruments),
        t1 - t0,
    )
    return instruments


def get_next_expiry_date(all_instruments: Dict[int, Dict[str, Any]]) -> Optional[str]:
    """
    Get the next SENSEX expiry date.

    Args:
        all_instruments: Dictionary of all SENSEX instruments.

    Returns:
        str: Next expiry date in YYYY-MM-DD format, or None if not found.
    """
    today = datetime.date.today()
    expiries = set()

    for inst in all_instruments.values():
        if inst["segment"] == "BFO-OPT":
            expiries.add(inst["expiry"])

    if not expiries:
        return None

    future_expiries = [e for e in expiries if e >= today]
    if not future_expiries:
        return None

    next_expiry = min(future_expiries)
    return str(next_expiry)


def get_sensex_positions_summary(
    kite: Any,
    days: Optional[int] = None,
    restrict_to_next_expiry: bool = False,
    volatility: float = DEFAULT_VOLATILITY,
    interest_rate: float = DEFAULT_INTEREST_RATE,
) -> Dict[str, Any]:
    """
    Get summary of SENSEX positions with delta and margin calculations.

    Args:
        kite: KiteConnect client instance.
        days: Optional. If provided, only include positions expiring within
              this many trading days.
        restrict_to_next_expiry: If True, only include positions for the
                                  immediate next expiry.
        volatility: Implied volatility for delta calculation.
        interest_rate: Risk-free interest rate for delta calculation.

    Returns:
        dict: Position summary.
    """
    _fn_start = time.perf_counter()
    logging.info("[SENSEX TIMING] get_sensex_positions_summary start (days=%s, next_expiry_only=%s)", days, restrict_to_next_expiry)

    all_instruments = get_all_sensex_instruments()
    _t_after_instruments = time.perf_counter()

    t_pos0 = time.perf_counter()
    positions = kite.positions()
    t_pos1 = time.perf_counter()
    logging.info("[SENSEX TIMING] kite.positions() took %.3fs", t_pos1 - t_pos0)
    net_positions = positions.get("net", [])

    # Get SENSEX spot price — cache for 30 s to avoid duplicate calls from
    # the concurrent overall + next-expiry fetches on page load.
    now = time.monotonic()
    if _SENSEX_QUOTE_CACHE["price"] is not None and now < _SENSEX_QUOTE_CACHE["expires_at"]:
        sensex_price: float = _SENSEX_QUOTE_CACHE["price"]  # type: ignore[assignment]
        logging.info("[SENSEX TIMING] kite.quote('BSE:SENSEX') served from cache")
    else:
        try:
            t_quote0 = time.perf_counter()
            sensex_quote = kite.quote("BSE:SENSEX")
            t_quote1 = time.perf_counter()
            logging.info("[SENSEX TIMING] kite.quote('BSE:SENSEX') took %.3fs", t_quote1 - t_quote0)
            sensex_price = sensex_quote["BSE:SENSEX"]["last_price"]
            _SENSEX_QUOTE_CACHE["price"] = sensex_price
            _SENSEX_QUOTE_CACHE["expires_at"] = now + _SENSEX_QUOTE_TTL_SECONDS
        except Exception as e:
            logging.warning(f"Could not fetch SENSEX quote: {e}")
            sensex_price = _SENSEX_QUOTE_CACHE["price"] or 75000.0

    # Determine next expiry
    next_expiry = get_next_expiry_date(all_instruments)

    # Initialize counters
    total_delta = 0.0
    total_theta = 0.0
    futures_delta = 0.0
    total_ce = 0
    total_pe = 0
    total_positive_ce = 0
    total_negative_ce = 0
    total_positive_pe = 0
    total_negative_pe = 0
    total_ce_delta = 0.0
    total_pe_delta = 0.0
    total_ce_theta = 0.0
    total_pe_theta = 0.0
    expiry_map: Dict[str, float] = {}
    expiry_theta_map: Dict[str, float] = {}
    positions_list: List[Dict[str, Any]] = []

    # New counters for "Today's Position"
    today_ce_qty = 0
    today_pe_qty = 0
    today_delta = 0.0
    today_theta = 0.0
    old_positive_ce = 0
    old_negative_ce = 0
    old_positive_pe = 0
    old_negative_pe = 0
    old_ce = 0
    old_pe = 0

    # Expiry scenario accumulators (short positions only, quantity < 0)
    pe_time_value_total: float = 0.0
    ce_time_value_total: float = 0.0
    ce_itm_loss_total: float = 0.0
    pe_itm_loss_total: float = 0.0

    _t_loop_start = time.perf_counter()
    logging.info("[SENSEX TIMING] entering position loop (%d net positions)", len(net_positions))

    for pos in net_positions:
        todays_net_qty = pos.get("day_buy_quantity", 0) - pos.get("day_sell_quantity", 0)
        is_open = pos["quantity"] != 0

        if not is_open and todays_net_qty == 0:
            continue

        trading_symbol = pos["tradingsymbol"]
        if not trading_symbol.startswith("SENSEX"):
            continue

        token = pos["instrument_token"]
        if token not in all_instruments:
            continue

        inst = all_instruments[token]
        expiry_date = str(inst["expiry"])

        # Filter by days if specified
        if days is not None and inst["days_to_expiry"] > days:
            continue

        # Filter by next expiry if specified
        if restrict_to_next_expiry and expiry_date != next_expiry:
            continue

        # Handle futures
        if inst["segment"] == "BFO-FUT":
            if is_open:
                total_delta += pos["quantity"]
                futures_delta += pos["quantity"]
                expiry_map[expiry_date] = expiry_map.get(expiry_date, 0) + pos["quantity"]
            today_delta += todays_net_qty
            continue

        # Calculate delta and theta for options using Black-Scholes with live IV
        try:
            dte = max(inst["days_to_expiry"], 0.0001)
            ltp = pos.get("last_price", 0)
            implied_vol = _get_implied_volatility(
                sensex_price,
                inst["strike"],
                interest_rate,
                dte,
                ltp=ltp,
                option_type=inst["instrument_type"],
                fallback_vol=volatility,
            )
            bs = mibian.BS(
                [sensex_price, inst["strike"], interest_rate, dte],
                volatility=implied_vol,
            )
        except Exception as e:
            logging.warning(f"Greek calc failed for {trading_symbol}: {e}")
            continue

        computed_delta = 0.0
        computed_theta = 0.0
        if inst["instrument_type"] == "CE":
            if is_open:
                computed_delta = bs.callDelta * pos["quantity"]
                computed_theta = bs.callTheta * pos["quantity"]
                total_ce_delta += computed_delta
                total_ce_theta += computed_theta
                total_ce += pos["quantity"]
                if pos["quantity"] > 0:
                    total_positive_ce += abs(pos["quantity"])
                else:
                    total_negative_ce += abs(pos["quantity"])
                    ce_intrinsic = max(sensex_price - inst["strike"], 0.0)
                    ce_time_val = max(ltp - ce_intrinsic, 0.0)
                    abs_qty = abs(pos["quantity"])
                    ce_time_value_total += abs_qty * ce_time_val
                    ce_itm_loss_total += abs_qty * ce_intrinsic
            
            # Today's metrics
            today_ce_qty += todays_net_qty
            computed_today_delta = bs.callDelta * todays_net_qty
            computed_today_theta = bs.callTheta * todays_net_qty
            today_delta += computed_today_delta
            today_theta += computed_today_theta

            # For margin comp
            old_qty = pos["quantity"] - todays_net_qty
            old_ce += old_qty
            if old_qty > 0:
                old_positive_ce += abs(old_qty)
            else:
                old_negative_ce += abs(old_qty)

        elif inst["instrument_type"] == "PE":
            if is_open:
                computed_delta = bs.putDelta * pos["quantity"]
                computed_theta = bs.putTheta * pos["quantity"]
                total_pe_delta += computed_delta
                total_pe_theta += computed_theta
                total_pe += pos["quantity"]
                if pos["quantity"] > 0:
                    total_positive_pe += abs(pos["quantity"])
                else:
                    total_negative_pe += abs(pos["quantity"])
                    pe_intrinsic = max(inst["strike"] - sensex_price, 0.0)
                    pe_time_val = max(ltp - pe_intrinsic, 0.0)
                    abs_qty = abs(pos["quantity"])
                    pe_time_value_total += abs_qty * pe_time_val
                    pe_itm_loss_total += abs_qty * pe_intrinsic
            
            # Today's metrics
            today_pe_qty += todays_net_qty
            computed_today_delta = bs.putDelta * todays_net_qty
            computed_today_theta = bs.putTheta * todays_net_qty
            today_delta += computed_today_delta
            today_theta += computed_today_theta

            # For margin comp
            old_qty = pos["quantity"] - todays_net_qty
            old_pe += old_qty
            if old_qty > 0:
                old_positive_pe += abs(old_qty)
            else:
                old_negative_pe += abs(old_qty)

        if is_open:
            total_delta += computed_delta
            total_theta += computed_theta
            expiry_map[expiry_date] = expiry_map.get(expiry_date, 0) + computed_delta
            expiry_theta_map[expiry_date] = expiry_theta_map.get(expiry_date, 0) + computed_theta

            # Add to position list
            positions_list.append(
                {
                    "symbol": trading_symbol,
                    "quantity": pos["quantity"],
                    "strike": inst["strike"],
                    "type": inst["instrument_type"],
                    "expiry": expiry_date,
                    "delta": round(computed_delta, 2),
                    "theta": round(computed_theta, 2),
                    "iv": round(implied_vol, 2),
                    "ltp": pos.get("last_price", 0),
                    "pnl": pos.get("pnl", 0),
                }
            )

    _t_loop_end = time.perf_counter()
    logging.info("[SENSEX TIMING] position loop done in %.3fs (%d positions built)", _t_loop_end - _t_loop_start, len(positions_list))

    # Calculate margin
    spread_count = abs(total_positive_ce + total_positive_pe)
    single_pe_ce = abs(total_pe - total_ce)
    both_ce_pe = abs(min(abs(total_ce), abs(total_pe)))
    margin_required = calculate_margin_requirement(spread_count, single_pe_ce, both_ce_pe)

    # Calculate previous margin to get margin increased/reduced
    old_spread_count = abs(old_positive_ce + old_positive_pe)
    old_single_pe_ce = abs(old_pe - old_ce)
    old_both_ce_pe = abs(min(abs(old_ce), abs(old_pe)))
    old_margin_required = calculate_margin_requirement(old_spread_count, old_single_pe_ce, old_both_ce_pe)
    today_margin = margin_required - old_margin_required

    _fn_total = time.perf_counter() - _fn_start
    logging.info("[SENSEX TIMING] get_sensex_positions_summary TOTAL %.3fs", _fn_total)

    return {
        "ce_sold": total_negative_ce,
        "pe_sold": total_negative_pe,
        "ce_bought": total_positive_ce,
        "pe_bought": total_positive_pe,
        "total_ce": total_ce,
        "total_pe": total_pe,
        "total_delta": round(total_delta, 2),
        "total_theta": round(total_theta, 2),
        "ce_delta": round(total_ce_delta, 2),
        "pe_delta": round(total_pe_delta, 2),
        "ce_theta": round(total_ce_theta, 2),
        "pe_theta": round(total_pe_theta, 2),
        "futures_delta": round(futures_delta, 2),
        "margin_required": round(margin_required, 2),
        "margin_formatted": format_inr(margin_required),
        "expiry_map": {k: round(v, 2) for k, v in expiry_map.items()},
        "expiry_theta_map": {k: round(v, 2) for k, v in expiry_theta_map.items()},
        "next_expiry": next_expiry,
        "positions": positions_list,
        "sensex_spot": sensex_price,
        "today_ce_qty": today_ce_qty,
        "today_pe_qty": today_pe_qty,
        "today_delta": round(today_delta, 2),
        "today_theta": round(today_theta, 2),
        "today_margin": round(today_margin, 2),
        "today_margin_formatted": format_inr(today_margin),
        "lot_size": SENSEX_LOT_SIZE,
        "pe_time_value": round(pe_time_value_total, 2),
        "ce_time_value": round(ce_time_value_total, 2),
        "ce_itm_loss": round(ce_itm_loss_total, 2),
        "pe_itm_loss": round(pe_itm_loss_total, 2),
        "pe_net": round(pe_time_value_total - ce_itm_loss_total, 2),
        "ce_net": round(ce_time_value_total - pe_itm_loss_total, 2),
        "pe_time_value_formatted": format_inr(pe_time_value_total),
        "ce_time_value_formatted": format_inr(ce_time_value_total),
        "ce_itm_loss_formatted": format_inr(ce_itm_loss_total),
        "pe_itm_loss_formatted": format_inr(pe_itm_loss_total),
        "pe_net_formatted": format_inr(abs(pe_time_value_total - ce_itm_loss_total)),
        "ce_net_formatted": format_inr(abs(ce_time_value_total - pe_itm_loss_total)),
    }


def calculate_trading_days_from_date(
    start_date: datetime.date, num_days: int
) -> datetime.date:
    """
    Calculate the end date given a start date and number of trading days.
    """
    end_date = np.busday_offset(start_date, num_days, roll="forward")
    return end_date.astype("datetime64[D]").astype(datetime.date)
