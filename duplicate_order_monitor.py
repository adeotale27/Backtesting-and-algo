"""Duplicate order detection and alert engine.

Scans all OPEN F&O orders from Kite and identifies groups where two or more
orders for the same instrument (same direction) have prices within 2% of each
other. This typically means multiple algos independently placed orders for the
same position — one of which should be cancelled before it fills and creates an
unintended over-sized position.

Primary entry points:
  - ``scan_duplicate_orders(kite_client)``  — used by Flask route (sync, returns list)
  - ``check_and_notify_duplicates()``       — called by APScheduler every 5 min
"""

import configparser
import itertools
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KNOWN_UNDERLYINGS = ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTY"]

_MONTHLY_MONTH_NAMES: dict[str, str] = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
}

# Zerodha weekly option month chars: 1-9 for Jan-Sep, O/N/D for Oct/Nov/Dec
_WEEKLY_MONTH_CHARS: dict[str, str] = {
    "1": "Jan", "2": "Feb", "3": "Mar", "4": "Apr", "5": "May",
    "6": "Jun", "7": "Jul", "8": "Aug", "9": "Sep",
    "O": "Oct", "N": "Nov", "D": "Dec",
}

_F_AND_O_EXCHANGES: frozenset[str] = frozenset({"NFO", "BFO"})
_PRICE_PROXIMITY_THRESHOLD: float = 0.02   # 2% midpoint difference
_NOTIFICATION_COOLDOWN_SECONDS: int = 900  # 15-minute cooldown per symbol+direction group

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_BASE_DIR, "configfile.ini")


# ---------------------------------------------------------------------------
# Symbol parsing
# ---------------------------------------------------------------------------


def parse_instrument_display(tradingsymbol: str) -> dict[str, str]:
    """Best-effort parse of an NSE/BSE F&O trading symbol into readable components.

    Handles three formats:
      - Monthly options:  UNDERLYING + YYMMM + STRIKE + CE/PE  (e.g. NIFTY25JUN25000CE)
      - Weekly options:   UNDERLYING + YY + MCHAR + DD + STRIKE + CE/PE
                          where MCHAR is 1-9 for Jan-Sep, O/N/D for Oct/Nov/Dec
                          (e.g. NIFTY2562625000CE → 26-Jun-25)
      - Futures:          UNDERLYING + YYMMM + FUT  (e.g. NIFTY25JUNFUT)

    Args:
        tradingsymbol: Raw Kite tradingsymbol string.

    Returns:
        Dict with keys: underlying, expiry_display, strike, option_type, display.
        Falls back to raw symbol string if no pattern matches.
    """
    fallback: dict[str, str] = {
        "underlying": tradingsymbol,
        "expiry_display": "",
        "strike": "",
        "option_type": "",
        "display": tradingsymbol,
    }

    underlying = ""
    rest = tradingsymbol
    for candidate in _KNOWN_UNDERLYINGS:
        if tradingsymbol.startswith(candidate):
            underlying = candidate
            rest = tradingsymbol[len(candidate):]
            break

    if not underlying:
        return fallback

    # Monthly options: YY + MMM (3-letter) + STRIKE + CE/PE/FUT
    monthly_match = re.match(
        r"^(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d*)(CE|PE|FUT)$",
        rest,
    )
    if monthly_match:
        yy, month_abbr, strike, option_type = monthly_match.groups()
        expiry_display = f"{month_abbr}-{yy}"
        parts = [underlying, expiry_display]
        if strike:
            parts.append(strike)
        parts.append(option_type)
        return {
            "underlying": underlying,
            "expiry_display": expiry_display,
            "strike": strike,
            "option_type": option_type,
            "display": " · ".join(parts),
        }

    # Weekly options: YY + single month char (1-9 or O/N/D) + 2-digit day + STRIKE + CE/PE
    weekly_match = re.match(r"^(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$", rest, re.IGNORECASE)
    if weekly_match:
        yy, month_char, day_str, strike, option_type = weekly_match.groups()
        month_name = _WEEKLY_MONTH_CHARS.get(month_char.upper(), month_char)
        expiry_display = f"{int(day_str):02d}-{month_name}-{yy}"
        return {
            "underlying": underlying,
            "expiry_display": expiry_display,
            "strike": strike,
            "option_type": option_type,
            "display": f"{underlying} · {expiry_display} · {strike} {option_type}",
        }

    # Futures: YY + MMM + FUT
    fut_match = re.match(
        r"^(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)FUT$",
        rest,
    )
    if fut_match:
        yy, month_abbr = fut_match.groups()
        expiry_display = f"{month_abbr}-{yy}"
        return {
            "underlying": underlying,
            "expiry_display": expiry_display,
            "strike": "",
            "option_type": "FUT",
            "display": f"{underlying} · {expiry_display} · FUT",
        }

    return fallback


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------


def _price_diff_pct(price_a: float, price_b: float) -> float:
    """Compute the midpoint percentage difference between two prices.

    Args:
        price_a: First price (must be > 0).
        price_b: Second price (must be > 0).

    Returns:
        Fractional absolute difference relative to midpoint (e.g. 0.012 = 1.2%).
        Returns 0.0 if the midpoint is zero.
    """
    midpoint = (price_a + price_b) / 2.0
    if midpoint == 0.0:
        return 0.0
    return abs(price_a - price_b) / midpoint


def scan_duplicate_orders(kite_client: Any) -> list[dict]:
    """Fetch OPEN F&O orders from Kite and return groups with price-proximity duplicates.

    Groups are keyed on (tradingsymbol, transaction_type). Within each group all
    order pairs are evaluated; the group is returned if any pair has a midpoint
    price difference ≤ 2%. Market orders (price == 0) are excluded.

    Args:
        kite_client: Authenticated KiteConnect or MonitoredKite instance.

    Returns:
        List of duplicate-group dicts, each containing:
          {
            "tradingsymbol": str,
            "instrument_display": str,
            "underlying": str,
            "expiry_display": str,
            "strike": str,
            "option_type": str,
            "transaction_type": str,       # "BUY" or "SELL"
            "max_price_diff_pct": float,   # worst-case pair diff as % (e.g. 1.2)
            "order_count": int,
            "orders": list[dict],          # enriched per-order dicts
          }
        Returns [] on API error (after logging).
    """
    from trade_journal import classify_source

    try:
        all_orders: list[dict] = kite_client.orders()
    except Exception as exc:
        logger.error("scan_duplicate_orders: kite.orders() failed — %s", exc)
        return []

    open_fno_orders = [
        order for order in all_orders
        if order.get("status") == "OPEN"
        and order.get("exchange") in _F_AND_O_EXCHANGES
        and float(order.get("price", 0) or 0) > 0
    ]

    # Group by (tradingsymbol, transaction_type)
    groups: dict[tuple[str, str], list[dict]] = {}
    for order in open_fno_orders:
        key = (str(order["tradingsymbol"]), str(order["transaction_type"]))
        groups.setdefault(key, []).append(order)

    duplicate_groups: list[dict] = []

    for (tradingsymbol, transaction_type), group_orders in groups.items():
        if len(group_orders) < 2:
            continue

        prices = [float(o.get("price", 0) or 0) for o in group_orders]
        pair_diffs = [
            _price_diff_pct(pa, pb)
            for pa, pb in itertools.combinations(prices, 2)
        ]
        max_diff_pct = max(pair_diffs)

        if max_diff_pct > _PRICE_PROXIMITY_THRESHOLD:
            continue

        instrument_info = parse_instrument_display(tradingsymbol)

        enriched_orders: list[dict] = []
        for order in group_orders:
            tag = (order.get("tag") or "Unknown").strip() or "Unknown"
            source_info = classify_source(tag)
            enriched_orders.append({
                "order_id": str(order.get("order_id", "")),
                "variety": str(order.get("variety", "regular") or "regular"),
                "price": float(order.get("price", 0) or 0),
                "quantity": int(order.get("quantity", 0) or 0),
                "filled_quantity": int(order.get("filled_quantity", 0) or 0),
                "status": str(order.get("status", "")),
                "tag": tag,
                "algo_name": source_info["algo_name"],
                "algo_category": source_info["category"],
                "is_algo": source_info["is_algo"],
                "placed_at": str(order.get("order_timestamp", "") or ""),
                "instrument_token": str(order.get("instrument_token", "") or ""),
                "segment": str(order.get("segment", "") or ""),
            })

        first_order = enriched_orders[0] if enriched_orders else {}
        duplicate_groups.append({
            "tradingsymbol": tradingsymbol,
            "instrument_display": instrument_info["display"],
            "underlying": instrument_info["underlying"],
            "expiry_display": instrument_info["expiry_display"],
            "strike": instrument_info["strike"],
            "option_type": instrument_info["option_type"],
            "transaction_type": transaction_type,
            "max_price_diff_pct": round(max_diff_pct * 100, 2),
            "order_count": len(enriched_orders),
            "orders": enriched_orders,
            "instrument_token": first_order.get("instrument_token", ""),
            "segment": first_order.get("segment", ""),
        })

    logger.info(
        "scan_duplicate_orders: %d OPEN F&O orders checked → %d duplicate group(s) found",
        len(open_fno_orders),
        len(duplicate_groups),
    )
    return duplicate_groups


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------


def _build_kite_client_from_db() -> Optional[Any]:
    """Construct a fresh KiteConnect instance using the stored session token.

    Follows the same pattern as other scheduler jobs: reads api_key from
    configfile.ini and access_token from instruments.db.

    Returns:
        Configured KiteConnect instance, or None if credentials are unavailable.
    """
    import instrument_cache
    from kiteconnect import KiteConnect

    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_PATH)
    api_key = cfg.get("kite_login_details", "api_key", fallback="").strip()
    if not api_key:
        logger.error("_build_kite_client_from_db: api_key missing in configfile.ini")
        return None

    access_token: Optional[str] = instrument_cache.get_kite_token()
    if not access_token:
        logger.warning("_build_kite_client_from_db: no stored access token")
        return None

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def check_and_notify_duplicates() -> None:
    """Scheduler entry point: scan for duplicate orders and fire notifications.

    - Runs only during market hours (9:20–15:25 IST, weekdays).
    - Uses a 15-minute per-group cooldown keyed on tradingsymbol + transaction_type
      to prevent notification spam when orders remain open across multiple scan cycles.
    - Each duplicate group fires one DUPLICATE_ORDER_ALERT notification.

    Never raises — all exceptions are caught and logged.
    """
    from common_lib import get_ist_now
    from notifications import database as notif_db
    from notifications.service import dispatch

    now_ist = get_ist_now()

    if now_ist.weekday() >= 5:
        logger.debug("check_and_notify_duplicates: weekend — skipping")
        return

    market_open = now_ist.replace(hour=9, minute=20, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=25, second=0, microsecond=0)
    if not (market_open <= now_ist <= market_close):
        logger.debug("check_and_notify_duplicates: outside market hours — skipping")
        return

    logger.info("check_and_notify_duplicates: running scan")

    kite_client = _build_kite_client_from_db()
    if kite_client is None:
        return

    try:
        duplicate_groups = scan_duplicate_orders(kite_client)
    except Exception as exc:
        logger.error("check_and_notify_duplicates: scan failed — %s", exc, exc_info=True)
        return

    for group in duplicate_groups:
        symbol = group["tradingsymbol"]
        txn = group["transaction_type"]
        cooldown_key = f"DUPLICATE_ORDER_{symbol}_{txn}"

        if not notif_db.check_and_update_cooldown(cooldown_key, _NOTIFICATION_COOLDOWN_SECONDS):
            logger.debug(
                "check_and_notify_duplicates: cooldown active for %s %s — suppressed",
                txn,
                symbol,
            )
            continue

        algo_names = list(dict.fromkeys(o["algo_name"] for o in group["orders"]))
        algos_str = " + ".join(algo_names)
        diff_pct = group["max_price_diff_pct"]
        order_count = group["order_count"]
        instrument_label = group["instrument_display"]

        title = f"Duplicate Orders: {symbol}"
        body = (
            f"{order_count} {txn} orders within {diff_pct:.1f}% — {algos_str}\n"
            f"{instrument_label}"
        )

        try:
            dispatch(
                notification_type="DUPLICATE_ORDER_ALERT",
                title=title,
                body=body,
                metadata={
                    "symbol": symbol,
                    "instrument_display": instrument_label,
                    "transaction_type": txn,
                    "order_count": order_count,
                    "max_price_diff_pct": diff_pct,
                    "algos": algo_names,
                    "action_url": "/duplicate-orders",
                    "action_label": "View Dashboard",
                },
            )
            logger.info(
                "check_and_notify_duplicates: dispatched DUPLICATE_ORDER_ALERT "
                "for %s %s (%.1f%% diff, %d orders, algos: %s)",
                txn,
                symbol,
                diff_pct,
                order_count,
                algos_str,
            )
        except Exception as exc:
            logger.error(
                "check_and_notify_duplicates: dispatch failed for %s — %s", symbol, exc
            )
