"""
Positions library module for NIFTY open positions dashboard.

Provides utility functions for calculating position data, delta, and margin
requirements for NIFTY options positions.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

import numpy as np

import instrument_cache

import greeks_lib as mibian

# Margin constants (per lot)
MARGIN_SPREAD = 43000
MARGIN_BOTH_PE_CE = 222000
MARGIN_SINGLE_PE_CE = 162000

# Import lot size from common_lib to stay in sync with the rest of the system
try:
    from common_lib import nifty_lot_size as NIFTY_LOT_SIZE
except ImportError:
    NIFTY_LOT_SIZE = 65  # Fallback default

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
    return margin / NIFTY_LOT_SIZE


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


def get_all_nifty_instruments(_kite: Any) -> Dict[int, Dict[str, Any]]:
    """
    Filter all NIFTY F&O instruments from the local SQLite cache.

    Reads from instrument_cache (instruments.db) which is refreshed daily at
    9 AM IST — no live Kite API call needed.

    Args:
        _kite: Unused; kept for backward-compatible call sites.

    Returns:
        dict: Mapping of instrument_token to instrument details.
    """
    all_fo_instruments = instrument_cache.get_all_fut_opt_instruments()
    today = datetime.date.today()
    nifty_instruments: Dict[int, Dict[str, Any]] = {}

    for token, inst in all_fo_instruments.items():
        symbol = inst["tradingsymbol"]
        if "NIFTY" not in symbol:
            continue
        if "MIDCPNIFTY" in symbol:
            continue
        if "FINNIFTY" in symbol:
            continue
        if inst["segment"] not in ("NFO-OPT", "NFO-FUT"):
            continue
        if inst.get("strike") is None:
            continue

        expiry_raw = inst["expiry"]
        expiry_date = datetime.date.fromisoformat(expiry_raw) if isinstance(expiry_raw, str) else expiry_raw

        nifty_instruments[token] = {
            "tradingsymbol": symbol,
            "expiry": expiry_date,
            "strike": inst["strike"],
            "instrument_type": inst["instrument_type"],
            "segment": inst["segment"],
            "exchange": inst["exchange"],
            "days_to_expiry": (expiry_date - today).days,
        }

    return nifty_instruments


def get_next_expiry_date(all_instruments: Dict[int, Dict[str, Any]]) -> Optional[str]:
    """
    Get the next NIFTY expiry date.

    Args:
        all_instruments: Dictionary of all NIFTY instruments.

    Returns:
        str: Next expiry date in YYYY-MM-DD format, or None if not found.
    """
    today = datetime.date.today()
    expiries = set()

    for inst in all_instruments.values():
        if inst["segment"] == "NFO-OPT":
            expiries.add(inst["expiry"])

    if not expiries:
        return None

    future_expiries = [e for e in expiries if e >= today]
    if not future_expiries:
        return None

    next_expiry = min(future_expiries)
    return str(next_expiry)


def get_nifty_positions_summary(
    kite: Any,
    days: Optional[int] = None,
    restrict_to_next_expiry: bool = False,
    volatility: float = DEFAULT_VOLATILITY,
    interest_rate: float = DEFAULT_INTEREST_RATE,
    min_premium: float = 0.0,
    prefetched_instruments: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Get summary of NIFTY positions with delta and margin calculations.

    Args:
        kite: KiteConnect client instance.
        days: Optional. If provided, only include positions expiring within
              this many trading days.
        restrict_to_next_expiry: If True, only include positions for the
                                  immediate next expiry.
        volatility: Implied volatility for delta calculation.
        interest_rate: Risk-free interest rate for delta calculation.

    Returns:
        dict: Position summary containing:
            - ce_sold: Total CE quantity sold (negative)
            - pe_sold: Total PE quantity sold (negative)
            - ce_bought: Total CE quantity bought (positive)
            - pe_bought: Total PE quantity bought (positive)
            - total_delta: Overall portfolio delta
            - ce_delta: CE positions delta contribution
            - pe_delta: PE positions delta contribution
            - margin_required: Estimated margin requirement
            - margin_formatted: Margin formatted as INR string
            - futures_delta: Delta from futures positions
            - expiry_map: Delta breakdown by expiry date
            - next_expiry: Next expiry date string
            - positions: List of individual position details
    """
    # Fetch instruments and positions
    all_instruments = prefetched_instruments if prefetched_instruments is not None else get_all_nifty_instruments(kite)
    positions = kite.positions()
    net_positions = positions.get("net", [])

    # Get NIFTY spot price
    try:
        nifty_quote = kite.quote("NSE:NIFTY 50")
        nifty_price = nifty_quote["NSE:NIFTY 50"]["last_price"]
    except Exception as e:
        logging.warning(f"Could not fetch NIFTY quote: {e}")
        nifty_price = 23000  # Fallback

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

    for pos in net_positions:
        todays_net_qty = pos.get("day_buy_quantity", 0) - pos.get("day_sell_quantity", 0)
        is_open = pos["quantity"] != 0

        if not is_open and todays_net_qty == 0:
            continue

        trading_symbol = pos["tradingsymbol"]
        if not trading_symbol.startswith("NIFTY"):
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

        # Filter by minimum current premium (LTP)
        if min_premium > 0 and pos.get("last_price", 0) < min_premium:
            continue

        # Handle futures
        if inst["segment"] == "NFO-FUT":
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
                nifty_price,
                inst["strike"],
                interest_rate,
                dte,
                ltp=ltp,
                option_type=inst["instrument_type"],
                fallback_vol=volatility,
            )
            bs = mibian.BS(
                [nifty_price, inst["strike"], interest_rate, dte],
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
                    ce_intrinsic = max(nifty_price - inst["strike"], 0.0)
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
                    pe_intrinsic = max(inst["strike"] - nifty_price, 0.0)
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
        "nifty_spot": nifty_price,
        "today_ce_qty": today_ce_qty,
        "today_pe_qty": today_pe_qty,
        "today_delta": round(today_delta, 2),
        "today_theta": round(today_theta, 2),
        "today_margin": round(today_margin, 2),
        "today_margin_formatted": format_inr(today_margin),
        "lot_size": NIFTY_LOT_SIZE,
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

    Args:
        start_date: Starting date.
        num_days: Number of trading days (weekdays) to add.

    Returns:
        datetime.date: End date after adding trading days.
    """
    end_date = np.busday_offset(start_date, num_days, roll="forward")
    return end_date.astype("datetime64[D]").astype(datetime.date)
