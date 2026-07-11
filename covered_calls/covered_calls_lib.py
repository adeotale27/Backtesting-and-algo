"""Covered calls management library.

Provides eligibility detection (holdings ≥ 70 % of F&O lot size), active
covered-call discovery (short CE positions in net positions), monthly expiry
calculation with a 7 working-day roll-forward rule, candidate strike
enumeration, and order placement helpers.
"""

from __future__ import annotations

import calendar
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Instruments DB helpers
# ---------------------------------------------------------------------------

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_BASE_DIR, "instruments.db")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Expiry calculation
# ---------------------------------------------------------------------------


def _last_tuesday_of_month(year: int, month: int) -> date:
    """Return the last Tuesday of the given month.

    NSE moved all monthly option expiries (both index and stock) to Tuesday
    as of 2025. The last Tuesday of the month is the monthly expiry date.

    Args:
        year: Four-digit year.
        month: Month number 1–12.

    Returns:
        Date of the last Tuesday.
    """
    last_day = calendar.monthrange(year, month)[1]
    candidate = date(year, month, last_day)
    while candidate.weekday() != 1:  # 1 = Tuesday
        candidate -= timedelta(days=1)
    return candidate


def _load_nse_holidays(year: int) -> set[date]:
    """Load NSE market holidays for the given year from instruments.db.

    Falls back to an empty set if the table is missing or empty (pre-sync).

    Args:
        year: Four-digit year.

    Returns:
        Set of holiday dates.
    """
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT holiday_date FROM market_holidays
            WHERE year = ? AND exchange IN ('NSE', 'NFO')
            """,
            (year,),
        )
        rows = cursor.fetchall()
        result: set[date] = set()
        for row in rows:
            try:
                result.add(date.fromisoformat(str(row["holiday_date"])))
            except (ValueError, TypeError):
                pass
        return result
    except sqlite3.OperationalError:
        logger.warning("market_holidays table missing — holiday-aware counting unavailable")
        return set()
    finally:
        conn.close()


def _adjust_for_holiday(candidate: date, holidays: set[date]) -> date:
    """Step back day-by-day from candidate until a non-holiday weekday is found.

    Args:
        candidate: Starting date (typically the last Thursday of a month).
        holidays: Set of NSE market holiday dates.

    Returns:
        The nearest prior (or same) non-holiday weekday.
    """
    while candidate.weekday() >= 5 or candidate in holidays:
        candidate -= timedelta(days=1)
    return candidate


def _count_working_days(start: date, end: date, holidays: set[date]) -> int:
    """Count Mon–Fri non-holiday days strictly between *start* and *end* (inclusive of end).

    Args:
        start: Start date (exclusive — today).
        end: End date (inclusive — the expiry).
        holidays: Set of NSE market holiday dates.

    Returns:
        Number of working days remaining.
    """
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            count += 1
        current += timedelta(days=1)
    return count


def get_available_expiries(
    months_ahead: int = 6,
    today: date | None = None,
) -> list[tuple[date, str, int, bool]]:
    """Return the next N monthly expiries for use in a dropdown selector.

    Args:
        months_ahead: How many consecutive months to return.
        today: Reference date. Defaults to current IST date.

    Returns:
        List of (expiry_date, display_label, working_days_remaining, is_recommended).
        is_recommended is True for the expiry that get_target_monthly_expiry() would pick.
    """
    if today is None:
        try:
            from common_lib import get_ist_now
            today = get_ist_now().date()
        except ImportError:
            from datetime import datetime
            today = datetime.today().date()

    all_holidays = _load_nse_holidays(today.year) | _load_nse_holidays(today.year + 1)

    def _candidate_expiry(year: int, month: int) -> date:
        raw = _last_tuesday_of_month(year, month)
        return _adjust_for_holiday(raw, all_holidays)

    recommended_expiry, _, _ = get_target_monthly_expiry(today)

    expiries: list[tuple[date, str, int, bool]] = []
    year, month = today.year, today.month
    for _ in range(months_ahead):
        expiry = _candidate_expiry(year, month)
        if expiry >= today:
            working_days = _count_working_days(today, expiry, all_holidays)
            is_recommended = expiry == recommended_expiry
            label = expiry.strftime("%b %Y").upper()
            expiries.append((expiry, label, working_days, is_recommended))

        month += 1
        if month > 12:
            month = 1
            year += 1

    return expiries


def get_target_monthly_expiry(today: date | None = None) -> tuple[date, str, int]:
    """Return the target monthly expiry for covered call selection.

    Uses the current month's last Thursday, but advances to next month's when
    fewer than 7 working days remain until the current expiry.

    Args:
        today: Reference date. Defaults to current IST date.

    Returns:
        Tuple of (expiry_date, display_label, working_days_remaining).
        display_label is e.g. "JUN 2026".
    """
    if today is None:
        try:
            from common_lib import get_ist_now
            today = get_ist_now().date()
        except ImportError:
            from datetime import datetime
            today = datetime.today().date()

    all_holidays = _load_nse_holidays(today.year) | _load_nse_holidays(today.year + 1)

    def _candidate_expiry(year: int, month: int) -> date:
        raw = _last_tuesday_of_month(year, month)
        return _adjust_for_holiday(raw, all_holidays)

    expiry = _candidate_expiry(today.year, today.month)
    working_days = _count_working_days(today, expiry, all_holidays)

    if working_days < 7:
        next_month = today.month % 12 + 1
        next_year = today.year + (1 if today.month == 12 else 0)
        expiry = _candidate_expiry(next_year, next_month)
        working_days = _count_working_days(today, expiry, all_holidays)

    display_label = expiry.strftime("%b %Y").upper()
    return expiry, display_label, working_days


# ---------------------------------------------------------------------------
# Holdings eligibility
# ---------------------------------------------------------------------------


@dataclass
class EligibleHolding:
    """A stock holding that meets the 70 % lot-size coverage threshold."""

    symbol: str
    exchange: str
    holding_qty: int
    lot_size: int
    coverage_pct: float
    eligible_lots: int
    ltp: float
    avg_price: float
    unrealised_pnl: float
    product: str


def _get_lot_size_for_symbol(symbol: str) -> int | None:
    """Return the F&O lot size for a stock.

    Tries FUT contracts first (any segment), then falls back to CE options.
    This handles stocks that have options but no listed futures, and avoids
    hard-coding a specific segment name (NFO-FUT vs others).

    Args:
        symbol: NSE equity symbol, e.g. "RELIANCE".

    Returns:
        Lot size integer, or None if no F&O contract exists in instruments.db.
    """
    conn = _get_db()
    try:
        cursor = conn.cursor()
        # Try futures first (nearest expiry, any segment)
        cursor.execute(
            """
            SELECT lot_size FROM instruments
            WHERE name = ?
              AND instrument_type = 'FUT'
            ORDER BY expiry ASC
            LIMIT 1
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row and row["lot_size"]:
            return int(row["lot_size"])

        # Fallback: get lot size from CE options (same field, always present)
        cursor.execute(
            """
            SELECT lot_size FROM instruments
            WHERE name = ?
              AND instrument_type = 'CE'
            ORDER BY expiry ASC
            LIMIT 1
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row and row["lot_size"]:
            logger.debug("lot_size for %s sourced from CE options (no FUT found)", symbol)
            return int(row["lot_size"])

        logger.warning("No F&O lot size found in instruments.db for %s — holding excluded", symbol)
        return None
    finally:
        conn.close()


@dataclass
class _RawHoldingAccumulator:
    """Accumulates per-symbol quantities across multiple product entries (CNC + MTF)."""

    exchange: str
    settled_qty: int = 0
    total_cost: float = 0.0          # sum of avg_price × settled_qty for weighted avg
    unrealised_pnl: float = 0.0
    ltp: float = 0.0
    products: list[str] = field(default_factory=list)


def _aggregate_holdings_by_symbol(
    raw_holdings: list[dict[str, Any]],
) -> dict[str, _RawHoldingAccumulator]:
    """Merge all product entries (CNC, MTF, etc.) for the same symbol into one bucket.

    Zerodha returns MTF and CNC positions for the same stock as separate rows in
    holdings(). This function collapses them so each stock produces exactly one
    eligibility check and one dashboard row.

    Quantity accounting per entry:
      settled = quantity + used_quantity + collateral_quantity - t1_quantity
        quantity            — freely available settled shares
        used_quantity       — shares pledged as MTF collateral (Margin Trading Facility)
        collateral_quantity — shares pledged as F&O margin collateral
        t1_quantity         — unsettled T+1 purchases (excluded — cannot back a CC yet)

    Args:
        raw_holdings: List of raw holding dicts from kite_client.holdings().

    Returns:
        Dict keyed by symbol → accumulated totals for NSE/BSE instruments only.
    """
    buckets: dict[str, _RawHoldingAccumulator] = {}

    for holding in raw_holdings:
        symbol: str = holding.get("tradingsymbol", "")
        if not symbol:
            continue

        exchange: str = holding.get("exchange", "")
        if exchange not in ("NSE", "BSE"):
            logger.debug("Skipping %s — exchange=%s not NSE/BSE", symbol, exchange)
            continue

        available_qty = int(holding.get("quantity", 0))
        pledged_qty   = int(holding.get("used_quantity", 0))
        collateral_qty = int(holding.get("collateral_quantity", 0))
        t1_qty         = int(holding.get("t1_quantity", 0))
        entry_settled  = available_qty + pledged_qty + collateral_qty - t1_qty

        if entry_settled <= 0:
            logger.debug(
                "Skipping %s %s entry — settled_qty=%d "
                "(qty=%d pledged=%d collateral=%d t1=%d)",
                symbol, holding.get("product", "?"),
                entry_settled, available_qty, pledged_qty, collateral_qty, t1_qty,
            )
            continue

        avg_price   = float(holding.get("average_price", 0) or 0)
        ltp         = float(holding.get("last_price", 0) or 0)
        entry_pnl   = float(holding.get("pnl", 0) or 0)
        product     = holding.get("product", "CNC")

        if symbol not in buckets:
            buckets[symbol] = _RawHoldingAccumulator(exchange=exchange)

        acc = buckets[symbol]
        acc.settled_qty     += entry_settled
        acc.total_cost      += avg_price * entry_settled
        acc.unrealised_pnl  += entry_pnl
        acc.ltp              = ltp          # same LTP for all product entries of a symbol
        if product not in acc.products:
            acc.products.append(product)

    return buckets


def get_eligible_holdings(kite_client: Any) -> list[EligibleHolding]:
    """Return holdings where settled quantity ≥ 70 % of the F&O lot size.

    Aggregates CNC and MTF (and any other product type) for the same symbol into
    a single entry before eligibility filtering. This prevents duplicate rows when
    a stock is held under both CNC and MTF simultaneously.

    Excludes T1 (unsettled) shares because they cannot legally back a covered call.

    Args:
        kite_client: Authenticated KiteConnect (or MonitoredKite) instance.

    Returns:
        List of EligibleHolding dataclass instances, sorted by symbol.

    Raises:
        Exception: Propagates any KiteConnect API error.
    """
    raw_holdings: list[dict[str, Any]] = kite_client.holdings()
    buckets = _aggregate_holdings_by_symbol(raw_holdings)

    eligible: list[EligibleHolding] = []

    for symbol, acc in buckets.items():
        lot_size = _get_lot_size_for_symbol(symbol)
        if lot_size is None or lot_size == 0:
            logger.info("Skipping %s — no F&O lot size found in instruments.db", symbol)
            continue

        settled_qty = acc.settled_qty
        if settled_qty < lot_size * 0.70:
            logger.info(
                "Skipping %s — settled_qty=%d < 70%% of lot_size=%d (%.1f%%)",
                symbol, settled_qty, lot_size, (settled_qty / lot_size) * 100,
            )
            continue

        coverage_pct  = round((settled_qty / lot_size) * 100, 1)
        eligible_lots = math.floor(settled_qty / lot_size)
        # Weighted-average buy price across CNC + MTF entries
        avg_price = round(acc.total_cost / settled_qty, 4) if settled_qty else 0.0
        product_label = "+".join(sorted(acc.products)) if acc.products else "CNC"

        eligible.append(
            EligibleHolding(
                symbol=symbol,
                exchange=acc.exchange,
                holding_qty=settled_qty,
                lot_size=lot_size,
                coverage_pct=coverage_pct,
                eligible_lots=eligible_lots,
                ltp=acc.ltp,
                avg_price=avg_price,
                unrealised_pnl=acc.unrealised_pnl,
                product=product_label,
            )
        )

    eligible.sort(key=lambda h: h.symbol)
    return eligible


# ---------------------------------------------------------------------------
# Active covered call detection
# ---------------------------------------------------------------------------


@dataclass
class CoveredCallLeg:
    """One individual CE position leg that contributes to a covered call."""

    tradingsymbol: str
    strike: float
    expiry: date | None
    lots: int
    short_qty: int
    avg_sell_price: float
    ltp: float
    unrealised_pnl: float


@dataclass
class ActiveCoveredCall:
    """Aggregated view of all short CE positions for one underlying stock.

    When a stock has CEs sold at multiple strikes or expiries, all legs are
    aggregated here: lots_covered and unrealised_pnl are sums; avg_sell_price
    is a quantity-weighted average. The individual legs are stored in `legs`.
    """

    symbol: str
    tradingsymbol: str      # primary leg tradingsymbol (first encountered)
    strike: float           # primary leg strike
    expiry: date | None     # primary leg expiry
    short_qty: int          # total across all legs
    lots_covered: int       # total across all legs
    avg_sell_price: float   # quantity-weighted average across all legs
    ltp: float              # primary leg LTP
    unrealised_pnl: float   # sum across all legs
    legs: list[CoveredCallLeg] = field(default_factory=list)


def _build_nfo_ce_name_map() -> dict[str, dict[str, Any]]:
    """Return a mapping of NFO CE tradingsymbol → instrument metadata.

    Used for a single-pass batch lookup rather than per-position queries.

    Returns:
        Dict keyed by tradingsymbol, values have 'name', 'strike', 'expiry', 'lot_size'.
    """
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT tradingsymbol, name, strike, expiry, lot_size
            FROM instruments
            WHERE instrument_type = 'CE'
              AND segment IN ('NFO-OPT', 'BFO-OPT')
            """
        )
        result: dict[str, dict[str, Any]] = {}
        for row in cursor.fetchall():
            ts = row["tradingsymbol"]
            try:
                expiry_date = date.fromisoformat(str(row["expiry"])) if row["expiry"] else None
            except (ValueError, TypeError):
                expiry_date = None
            result[ts] = {
                "name": row["name"],
                "strike": float(row["strike"] or 0),
                "expiry": expiry_date,
                "lot_size": int(row["lot_size"] or 1),
            }
        return result
    finally:
        conn.close()


def get_active_covered_calls(kite_client: Any) -> dict[str, ActiveCoveredCall]:
    """Scan net positions for short CE positions that represent covered calls.

    All CE legs for the same underlying are aggregated into a single
    ActiveCoveredCall entry. This handles stocks where calls were sold at
    multiple strikes or expiries simultaneously.

    Args:
        kite_client: Authenticated KiteConnect instance.

    Returns:
        Dict keyed by underlying stock symbol. Each value is an ActiveCoveredCall
        whose lots_covered and unrealised_pnl are summed across all CE legs.
    """
    positions: dict[str, list[dict[str, Any]]] = kite_client.positions()
    net_positions: list[dict[str, Any]] = positions.get("net", [])

    # Build instrument metadata map once (avoids N+1 DB queries)
    instrument_map = _build_nfo_ce_name_map()

    active: dict[str, ActiveCoveredCall] = {}

    for pos in net_positions:
        ts: str = pos.get("tradingsymbol", "")
        qty: int = int(pos.get("quantity", 0))

        # We want short (negative quantity) CE positions on NFO/BFO
        if qty >= 0:
            continue
        if pos.get("exchange", "") not in ("NFO", "BFO"):
            continue
        if not ts.endswith("CE"):
            continue

        meta = instrument_map.get(ts)
        if not meta:
            continue

        underlying: str = meta["name"]
        if not underlying:
            continue

        lot_size: int = meta["lot_size"]
        leg_lots: int = abs(qty) // lot_size if lot_size else 0
        leg_avg_price: float = float(pos.get("average_price", 0) or 0)
        leg_ltp: float = float(pos.get("last_price", 0) or 0)
        leg_pnl: float = float(pos.get("pnl", 0) or 0)

        leg = CoveredCallLeg(
            tradingsymbol=ts,
            strike=meta["strike"],
            expiry=meta["expiry"],
            lots=leg_lots,
            short_qty=abs(qty),
            avg_sell_price=leg_avg_price,
            ltp=leg_ltp,
            unrealised_pnl=leg_pnl,
        )

        if underlying in active:
            # Aggregate this leg into the existing entry
            existing = active[underlying]
            existing.legs.append(leg)
            existing.short_qty += leg.short_qty
            existing.lots_covered += leg.lots
            existing.unrealised_pnl += leg.unrealised_pnl
            total_qty = sum(l.short_qty for l in existing.legs)
            existing.avg_sell_price = (
                sum(l.avg_sell_price * l.short_qty for l in existing.legs) / total_qty
                if total_qty else 0.0
            )
        else:
            active[underlying] = ActiveCoveredCall(
                symbol=underlying,
                tradingsymbol=ts,
                strike=meta["strike"],
                expiry=meta["expiry"],
                short_qty=leg.short_qty,
                lots_covered=leg.lots,
                avg_sell_price=leg_avg_price,
                ltp=leg_ltp,
                unrealised_pnl=leg_pnl,
                legs=[leg],
            )

    return active


# ---------------------------------------------------------------------------
# Candidate strike enumeration
# ---------------------------------------------------------------------------


@dataclass
class CallCandidate:
    """An OTM CE option candidate for a covered call."""

    tradingsymbol: str
    instrument_token: int
    strike: float
    expiry: date
    lot_size: int
    option_ltp: float
    premium_per_lot: float
    annualized_yield_pct: float
    days_to_expiry: int
    otm_pct: float
    bid: float = 0.0
    ask: float = 0.0
    mid_price: float = 0.0
    meets_min_premium: bool = True
    meets_otm_pct: bool = True


def _get_ce_options_for_expiry(symbol: str, expiry: date) -> list[dict[str, Any]]:
    """Return all CE options for a symbol and expiry from instruments.db.

    Args:
        symbol: Underlying stock name, e.g. "TCS".
        expiry: Target expiry date.

    Returns:
        List of dicts with tradingsymbol, instrument_token, strike, lot_size.
    """
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT tradingsymbol, instrument_token, strike, lot_size
            FROM instruments
            WHERE name = ?
              AND instrument_type = 'CE'
              AND segment IN ('NFO-OPT', 'BFO-OPT')
              AND expiry = ?
            ORDER BY strike ASC
            """,
            (symbol, expiry.isoformat()),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_monthly_expiry_ce_candidates(
    kite_client: Any,
    symbol: str,
    ltp: float,
    lot_size: int,
    expiry: date,
    otm_pct: float,
    min_premium_per_lot: float,
) -> list[CallCandidate]:
    """Return OTM CE candidates for the target monthly expiry.

    Fetches a window of strikes from `otm_pct` % above LTP to `otm_pct + 5` %
    above LTP, queries live LTPs in a single batch call, and filters by the
    minimum premium threshold.

    Args:
        kite_client: Authenticated KiteConnect instance.
        symbol: Underlying stock name, e.g. "INFY".
        ltp: Current price of the underlying stock.
        lot_size: F&O lot size for the stock.
        expiry: Target monthly expiry date.
        otm_pct: Minimum OTM percentage (e.g. 7.0 for 7 % above LTP).
        min_premium_per_lot: Minimum option premium in ₹ per lot.

    Returns:
        List of CallCandidate instances sorted by strike ascending. Empty list
        if no options are found or none meet the premium threshold.
    """
    if ltp <= 0:
        logger.warning("get_monthly_expiry_ce_candidates: ltp=0 for %s — skipping", symbol)
        return []

    all_options = _get_ce_options_for_expiry(symbol, expiry)
    if not all_options:
        logger.info("No CE options found in DB for %s expiry=%s", symbol, expiry)
        return []

    min_strike = ltp * (1 + otm_pct / 100)

    # Include ALL strikes strictly above spot so the user can navigate freely.
    # Strikes below the OTM floor are marked meets_otm_pct=False (visual only).
    window = [opt for opt in all_options if float(opt["strike"]) > ltp]

    if not window:
        logger.info("No OTM strikes found for %s @ %.1f%% OTM", symbol, otm_pct)
        return []

    # Batch quote fetch (gives bid/ask + LTP in one call)
    quote_keys = [f"NFO:{opt['tradingsymbol']}" for opt in window]
    try:
        quote_data: dict[str, dict[str, Any]] = kite_client.quote(quote_keys)
    except Exception:
        logger.warning("kite.quote() failed for %s — falling back to ltp()", symbol)
        try:
            ltp_fallback: dict[str, dict[str, Any]] = kite_client.ltp(quote_keys)
            quote_data = {k: {"last_price": v.get("last_price", 0)} for k, v in ltp_fallback.items()}
        except Exception:
            logger.exception("kite.ltp() fallback also failed for %s candidates", symbol)
            return []

    try:
        from common_lib import get_ist_now
        today_date = get_ist_now().date()
    except ImportError:
        from datetime import datetime
        today_date = datetime.today().date()

    days_to_expiry = max(1, (expiry - today_date).days)

    results: list[CallCandidate] = []
    for opt in window:
        key = f"NFO:{opt['tradingsymbol']}"
        option_quote = quote_data.get(key, {})
        option_ltp = float(option_quote.get("last_price", 0) or 0)

        # Extract best bid/ask from market depth
        depth = option_quote.get("depth", {})
        buy_levels: list[dict[str, Any]] = depth.get("buy", [])
        sell_levels: list[dict[str, Any]] = depth.get("sell", [])
        best_bid = float(buy_levels[0]["price"]) if buy_levels and buy_levels[0].get("price") else 0.0
        best_ask = float(sell_levels[0]["price"]) if sell_levels and sell_levels[0].get("price") else 0.0

        # Require a live two-sided market; skips illiquid options and after-hours depth
        if best_bid <= 0 or best_ask <= 0:
            continue

        mid_price = round((best_bid + best_ask) / 2, 2)

        effective_lot = int(opt["lot_size"]) if opt["lot_size"] else lot_size
        premium_per_lot = mid_price * effective_lot
        meets_premium = premium_per_lot >= min_premium_per_lot

        annualized_yield = (mid_price / ltp) * (365 / days_to_expiry) * 100
        strike = float(opt["strike"])
        otm_from_ltp = ((strike - ltp) / ltp) * 100
        meets_otm = strike >= min_strike

        results.append(
            CallCandidate(
                tradingsymbol=opt["tradingsymbol"],
                instrument_token=int(opt["instrument_token"]),
                strike=strike,
                expiry=expiry,
                lot_size=effective_lot,
                option_ltp=option_ltp,
                premium_per_lot=round(premium_per_lot, 2),
                annualized_yield_pct=round(annualized_yield, 2),
                days_to_expiry=days_to_expiry,
                otm_pct=round(otm_from_ltp, 2),
                bid=best_bid,
                ask=best_ask,
                mid_price=mid_price,
                meets_min_premium=meets_premium,
                meets_otm_pct=meets_otm,
            )
        )

    results.sort(key=lambda c: c.strike)
    return results


# ---------------------------------------------------------------------------
# Dashboard data aggregation
# ---------------------------------------------------------------------------


@dataclass
class CoveredCallRow:
    """One row in the covered calls dashboard."""

    symbol: str
    exchange: str
    holding_qty: int
    lot_size: int
    coverage_pct: float
    eligible_lots: int
    ltp: float
    avg_price: float
    unrealised_pnl: float
    product: str
    status: str  # "ACTIVE" | "PARTIAL" | "MISSING" | "PENDING_FILL"
    active_cc: ActiveCoveredCall | None
    candidates: list[CallCandidate] = field(default_factory=list)
    pending_order: dict | None = None  # set when status == "PENDING_FILL"


def _fetch_live_order_status_map(kite_client: Any) -> dict[str, str] | None:
    """Fetch all of today's Kite orders and return a map of order_id → status.

    Returns:
        Dict mapping order_id to status string, or None if the API call failed.
        Callers must treat None as "keep all pending orders as-is" (fail-safe).
    """
    try:
        orders: list[dict] = kite_client.orders() or []
        return {o["order_id"]: o["status"] for o in orders}
    except Exception:
        logger.warning("Could not fetch live orders for pending fill check", exc_info=True)
        return None


def _resolve_pending_order_status(
    order_id: str,
    live_order_status_map: dict[str, str] | None,
) -> str:
    """Resolve the effective Kite status of a pending order using the live orders map.

    An order absent from today's map is STALE — it is from a previous trading
    session and Kite has already discarded it.

    Args:
        order_id: Order ID stored in pending_orders_db.
        live_order_status_map: Output of _fetch_live_order_status_map(), or None on failure.

    Returns:
        Status string: one of "OPEN", "COMPLETE", "CANCELLED", "REJECTED",
        "STALE", or "UNKNOWN".
    """
    if live_order_status_map is None:
        return "UNKNOWN"  # API failed — fail-safe: keep pending
    if order_id not in live_order_status_map:
        return "STALE"  # Not in today's session → expired from a previous day
    return live_order_status_map[order_id]


def build_dashboard_data(
    kite_client: Any,
    otm_pct: float,
    min_premium_per_lot: float,
    expiry_override: date | None = None,
) -> tuple[list[CoveredCallRow], date, str, int]:
    """Build the full covered calls dashboard payload.

    Args:
        kite_client: Authenticated KiteConnect instance.
        otm_pct: OTM percentage for strike selection.
        min_premium_per_lot: Minimum premium in ₹ per lot.
        expiry_override: If provided, use this expiry instead of the auto-selected one.

    Returns:
        Tuple of (rows, target_expiry, display_label, working_days_remaining).
    """
    if expiry_override is not None:
        try:
            from common_lib import get_ist_now
            today = get_ist_now().date()
        except ImportError:
            from datetime import datetime
            today = datetime.today().date()
        all_holidays = _load_nse_holidays(today.year) | _load_nse_holidays(today.year + 1)
        working_days = _count_working_days(today, expiry_override, all_holidays)
        display_label = expiry_override.strftime("%b %Y").upper()
        target_expiry = expiry_override
    else:
        target_expiry, display_label, working_days = get_target_monthly_expiry()
    from covered_calls import pending_orders_db  # local import avoids circular dep

    holdings = get_eligible_holdings(kite_client)
    active_ccs = get_active_covered_calls(kite_client)
    live_order_status_map = _fetch_live_order_status_map(kite_client)

    rows: list[CoveredCallRow] = []

    for holding in holdings:
        active = active_ccs.get(holding.symbol)

        if active is None:
            status = "MISSING"
        elif active.lots_covered >= holding.eligible_lots:
            status = "ACTIVE"
        else:
            status = "PARTIAL"

        pending_order_data: dict | None = None

        # Check for an in-flight order when the position still shows MISSING
        if status == "MISSING":
            pending = pending_orders_db.get_pending_order(holding.symbol)
            if pending:
                kite_status = _resolve_pending_order_status(
                    pending["order_id"], live_order_status_map
                )

                if kite_status == "COMPLETE":
                    pending_orders_db.delete_pending_order(pending["order_id"])
                    # Position will appear ACTIVE on next load via get_active_covered_calls()
                elif kite_status in ("CANCELLED", "REJECTED", "STALE"):
                    pending_orders_db.delete_pending_order(pending["order_id"])
                    logger.info(
                        "Pending order %s for %s is %s — removed from pending",
                        pending["order_id"], holding.symbol, kite_status,
                    )
                else:
                    # OPEN / TRIGGER PENDING / PENDING / UNKNOWN — still in-flight
                    status = "PENDING_FILL"
                    pending_order_data = dict(pending)

        candidates: list[CallCandidate] = []
        if status == "PARTIAL" and holding.ltp > 0:
            try:
                candidates = get_monthly_expiry_ce_candidates(
                    kite_client=kite_client,
                    symbol=holding.symbol,
                    ltp=holding.ltp,
                    lot_size=holding.lot_size,
                    expiry=target_expiry,
                    otm_pct=otm_pct,
                    min_premium_per_lot=min_premium_per_lot,
                )
            except Exception:
                logger.exception("Candidate fetch failed for %s", holding.symbol)
        elif status == "MISSING" and holding.ltp > 0:
            try:
                candidates = get_monthly_expiry_ce_candidates(
                    kite_client=kite_client,
                    symbol=holding.symbol,
                    ltp=holding.ltp,
                    lot_size=holding.lot_size,
                    expiry=target_expiry,
                    otm_pct=otm_pct,
                    min_premium_per_lot=min_premium_per_lot,
                )
            except Exception:
                logger.exception("Candidate fetch failed for %s", holding.symbol)

        rows.append(
            CoveredCallRow(
                symbol=holding.symbol,
                exchange=holding.exchange,
                holding_qty=holding.holding_qty,
                lot_size=holding.lot_size,
                coverage_pct=holding.coverage_pct,
                eligible_lots=holding.eligible_lots,
                ltp=holding.ltp,
                avg_price=holding.avg_price,
                unrealised_pnl=holding.unrealised_pnl,
                product=holding.product,
                status=status,
                active_cc=active,
                candidates=candidates,
                pending_order=pending_order_data,
            )
        )

    _status_order = {"MISSING": 0, "PARTIAL": 1, "PENDING_FILL": 2, "ACTIVE": 3}
    rows.sort(key=lambda r: (_status_order.get(r.status, 9), r.symbol))
    return rows, target_expiry, display_label, working_days


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------


def place_covered_call(
    kite_client: Any,
    tradingsymbol: str,
    quantity: int,
    limit_price: float,
    underlying_symbol: str,
) -> dict[str, Any]:
    """Place a SELL CE limit order for a covered call.

    Args:
        kite_client: Authenticated KiteConnect instance.
        tradingsymbol: NFO option tradingsymbol, e.g. "TCS25JUN3400CE".
        quantity: Number of shares (lots × lot_size).
        limit_price: Limit price for the sell order.
        underlying_symbol: Underlying stock symbol for tagging, e.g. "TCS".

    Returns:
        Dict with 'order_id' on success.

    Raises:
        ValueError: If quantity or limit_price are invalid.
        Exception: Propagates KiteConnect API errors.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if limit_price <= 0:
        raise ValueError(f"limit_price must be positive, got {limit_price}")

    tag = f"CC-{underlying_symbol}"[:20]  # Zerodha tag limit is 20 chars

    order_id: str = kite_client.place_order(
        variety="regular",
        exchange="NFO",
        tradingsymbol=tradingsymbol,
        transaction_type="SELL",
        quantity=quantity,
        product="NRML",
        price=limit_price,
        order_type="LIMIT",
        tag=tag,
    )

    logger.info(
        "Covered call placed: %s qty=%d price=%.2f order_id=%s",
        tradingsymbol,
        quantity,
        limit_price,
        order_id,
    )

    # Persist to daily order log for trade journal attribution
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from common_lib import save_executed_order
        save_executed_order(
            symbol=tradingsymbol,
            transaction_type="SELL",
            price=limit_price,
            quantity=quantity,
            order_segment="NFO-OPT",
            algo_source="CoveredCall",
            order_id=str(order_id),
        )
    except Exception:
        logger.exception("save_executed_order failed for covered call %s — order was still placed", tradingsymbol)

    return {"order_id": order_id}


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _candidate_to_dict(c: CallCandidate) -> dict[str, Any]:
    """Serialise a CallCandidate to a JSON-safe dict."""
    return {
        "tradingsymbol": c.tradingsymbol,
        "strike": c.strike,
        "expiry": c.expiry.isoformat() if c.expiry else None,
        "lot_size": c.lot_size,
        "option_ltp": c.option_ltp,
        "bid": c.bid,
        "ask": c.ask,
        "premium_per_lot": c.premium_per_lot,
        "annualized_yield_pct": c.annualized_yield_pct,
        "days_to_expiry": c.days_to_expiry,
        "otm_pct": c.otm_pct,
        "meets_min_premium": c.meets_min_premium,
        "meets_otm_pct": c.meets_otm_pct,
    }


def _active_cc_to_dict(a: ActiveCoveredCall) -> dict[str, Any]:
    """Serialise an ActiveCoveredCall to a JSON-safe dict."""
    return {
        "tradingsymbol": a.tradingsymbol,
        "strike": a.strike,
        "expiry": a.expiry.isoformat() if a.expiry else None,
        "short_qty": a.short_qty,
        "lots_covered": a.lots_covered,
        "avg_sell_price": a.avg_sell_price,
        "ltp": a.ltp,
        "unrealised_pnl": a.unrealised_pnl,
        "legs": [
            {
                "tradingsymbol": l.tradingsymbol,
                "strike": l.strike,
                "expiry": l.expiry.isoformat() if l.expiry else None,
                "lots": l.lots,
                "short_qty": l.short_qty,
                "avg_sell_price": l.avg_sell_price,
                "ltp": l.ltp,
                "unrealised_pnl": l.unrealised_pnl,
            }
            for l in a.legs
        ],
    }


def row_to_dict(row: CoveredCallRow) -> dict[str, Any]:
    """Serialise a CoveredCallRow to a JSON-safe dict.

    Args:
        row: CoveredCallRow dataclass instance.

    Returns:
        JSON-serialisable dictionary.
    """
    return {
        "symbol": row.symbol,
        "exchange": row.exchange,
        "holding_qty": row.holding_qty,
        "lot_size": row.lot_size,
        "coverage_pct": row.coverage_pct,
        "eligible_lots": row.eligible_lots,
        "ltp": row.ltp,
        "avg_price": row.avg_price,
        "unrealised_pnl": row.unrealised_pnl,
        "product": row.product,
        "status": row.status,
        "active_cc": _active_cc_to_dict(row.active_cc) if row.active_cc else None,
        "candidates": [_candidate_to_dict(c) for c in row.candidates],
        "pending_order": row.pending_order,
    }
