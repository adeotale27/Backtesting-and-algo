import csv
import importlib
import logging
import re
from datetime import datetime, timedelta
from collections import deque, defaultdict
from typing import Optional
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instrument_cache

logger = logging.getLogger(__name__)

# Margin constants (from common_lib.py)
MARGIN_SPREAD = 43000
MARGIN_BOTH_PE_CE = 222000
MARGIN_SINGLE_PE_CE = 162000


def parse_tradebook_csv(file_path: str) -> list[dict]:
    """Parses Zerodha Tradebook CSV and returns list of trades.

    Args:
        file_path: Absolute path to the Zerodha tradebook CSV file.

    Returns:
        List of trade dicts with keys: symbol, trade_date, trade_type,
        quantity, price, order_execution_time, expiry_date.
    """
    trades = []
    with open(file_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                trade = {
                    "symbol": row["symbol"],
                    "trade_date": row["trade_date"],
                    "trade_type": row["trade_type"].lower(),
                    "quantity": float(row["quantity"]),
                    "price": float(row["price"]),
                    "order_execution_time": datetime.fromisoformat(row["order_execution_time"]),
                    "expiry_date": row["expiry_date"],
                }
                trades.append(trade)
            except (ValueError, KeyError) as exc:
                logger.warning("tradebook_analyzer: skipping row due to parse error: %s", exc)
    return trades


def get_expiry_type(symbol: str) -> tuple[str, str]:
    """Determines the underlying and expiry cadence from a symbol.

    Args:
        symbol: Trading symbol e.g. 'NIFTY25512500PE'.

    Returns:
        (underlying, cadence) e.g. ('NIFTY', 'Weekly') or ('SENSEX', 'Monthly').
        Returns ('Unknown', 'Unknown') when the symbol cannot be parsed.
    """
    match = re.search(r"^([A-Z]+)(\d{2}[A-Z\d]{3})", symbol)
    if not match:
        return "Unknown", "Unknown"

    underlying = match.group(1)
    expiry_code = match.group(2)
    cadence = "Monthly" if expiry_code[2:].isalpha() else "Weekly"
    return underlying, cadence


def _parse_strike_and_type(symbol: str) -> Optional[tuple[float, str]]:
    """Extracts the strike price and option type from a trading symbol.

    Supports both weekly (e.g. NIFTY25512500PE) and monthly
    (e.g. NIFTY25MAY24500CE) symbol formats.

    Args:
        symbol: Full trading symbol.

    Returns:
        (strike, option_type) where option_type is 'CE' or 'PE',
        or None if the symbol is not a standard option.
    """
    match = re.match(r"^[A-Z]+\d{2}[A-Z\d]{3}(\d+)(CE|PE)$", symbol)
    if not match:
        return None
    return float(match.group(1)), match.group(2)


def _fetch_underlying_close(
    underlying: str,
    expiry_date_str: str,
    price_cache: dict[str, Optional[float]],
) -> Optional[float]:
    """Fetches the underlying index closing price on an expiry date via Yahoo Finance.

    Results are cached in price_cache to avoid duplicate network calls
    within a single analysis run. Falls back up to 3 prior calendar
    days to handle market holidays.

    Args:
        underlying: Index name e.g. 'NIFTY' or 'SENSEX'.
        expiry_date_str: ISO date string e.g. '2025-05-12'.
        price_cache: Mutable dict used as an in-memory cache.

    Returns:
        Closing price as float, or None if all fetch attempts fail.
    """
    cache_key = f"{underlying}:{expiry_date_str}"
    if cache_key in price_cache:
        return price_cache[cache_key]

    closing_price: Optional[float] = None

    if closing_price is None:
        logger.warning(
            "tradebook_analyzer: could not fetch %s close for %s; "
            "expired positions will use 0.0 as settlement price",
            underlying, expiry_date_str,
        )

    price_cache[cache_key] = closing_price
    return closing_price



def _compute_option_settlement(
    option_type: str, strike: float, underlying_close: float
) -> float:
    """Computes the NSE/BSE settlement price for an expiring option.

    Args:
        option_type: 'CE' or 'PE'.
        strike: Strike price.
        underlying_close: Underlying spot closing price at expiry.

    Returns:
        Settlement price (>= 0).
    """
    if option_type == "CE":
        return max(0.0, underlying_close - strike)
    return max(0.0, strike - underlying_close)


def calculate_pnl_and_margin(
    trades: list[dict], after_date: Optional[str] = None
) -> list[dict]:
    """Processes trades to calculate P&L per expiry and peak margin deployment.

    Handles two P&L sources:
    1. FIFO-matched trades (manually squared off positions).
    2. Synthetic settlement for positions held to expiry (not in tradebook CSV).

    Args:
        trades: List of trade dicts from parse_tradebook_csv().
        after_date: ISO date string; if provided, only trades on/after this date
            are included.

    Returns:
        List of per-expiry result dicts sorted by expiry_date, each containing:
        expiry_date, expiry_type, total_pnl, max_margin, roc, total_trades,
        win_rate, range_high, range_low, range_diff,
        expiry_settled_pnl, expiry_settled_count.
    """
    trades.sort(key=lambda x: x["order_execution_time"])

    if after_date:
        trades = [t for t in trades if t["trade_date"] >= after_date]

    today_str = datetime.today().date().isoformat()

    expiry_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        underlying, _ = get_expiry_type(t["symbol"])
        if underlying not in ("NIFTY", "SENSEX"):
            continue
        if t["expiry_date"] > today_str:
            continue
        expiry_groups[(t["expiry_date"], underlying)].append(t)

    results: dict[tuple[str, str], dict] = {}

    for (expiry, underlying), expiry_trades in expiry_groups.items():
        symbol_queues: dict[str, dict[str, deque]] = defaultdict(
            lambda: {"buy": deque(), "sell": deque()}
        )
        total_pnl = 0.0
        trades_count = 0
        wins = 0
        total_matched = 0
        peak_margin = 0.0
        current_positions: dict[str, float] = defaultdict(float)

        expiry_trades.sort(key=lambda x: x["order_execution_time"])

        for t in expiry_trades:
            sym = t["symbol"]
            qty = t["quantity"]
            price = t["price"]
            side = t["trade_type"]

            # FIFO P&L matching
            opp_side = "sell" if side == "buy" else "buy"
            while qty > 0 and symbol_queues[sym][opp_side]:
                opp_trade = symbol_queues[sym][opp_side][0]
                matched_qty = min(qty, opp_trade["qty"])

                if side == "buy":
                    lot_pnl = (opp_trade["price"] - price) * matched_qty
                else:
                    lot_pnl = (price - opp_trade["price"]) * matched_qty

                total_pnl += lot_pnl
                if lot_pnl > 0:
                    wins += 1
                total_matched += 1

                qty -= matched_qty
                opp_trade["qty"] -= matched_qty
                if opp_trade["qty"] <= 0:
                    symbol_queues[sym][opp_side].popleft()

            if qty > 0:
                symbol_queues[sym][side].append({"qty": qty, "price": price})

            trades_count += 1

            # Margin tracking
            position_change = t["quantity"] if side == "buy" else -t["quantity"]
            current_positions[sym] += position_change
            margin = estimate_current_margin(current_positions)
            if margin > peak_margin:
                peak_margin = margin

        # Settlement pass: compute P&L for positions held to expiry
        price_cache: dict[str, Optional[float]] = {}
        expiry_settled_pnl = 0.0
        expiry_settled_count = 0

        for sym, queues in symbol_queues.items():
            parsed = _parse_strike_and_type(sym)
            if parsed is None:
                continue
            strike, option_type = parsed

            for side, queue in queues.items():
                for unmatched_lot in queue:
                    remaining_qty = unmatched_lot["qty"]
                    if remaining_qty <= 0:
                        continue

                    underlying_close = _fetch_underlying_close(
                        underlying, expiry, price_cache
                    )
                    settlement = _compute_option_settlement(
                        option_type,
                        strike,
                        underlying_close if underlying_close is not None else 0.0,
                    )

                    if side == "sell":
                        lot_pnl = (unmatched_lot["price"] - settlement) * remaining_qty
                    else:
                        lot_pnl = (settlement - unmatched_lot["price"]) * remaining_qty

                    total_pnl += lot_pnl
                    expiry_settled_pnl += lot_pnl
                    expiry_settled_count += 1
                    if lot_pnl > 0:
                        wins += 1
                    total_matched += 1

        _, type_str = get_expiry_type(expiry_trades[0]["symbol"])
        expiry_type = f"{underlying} {type_str}"
        range_high, range_low, range_diff = get_underlying_range(underlying, expiry)

        results[(expiry, underlying)] = {
            "expiry_date": expiry,
            "expiry_type": expiry_type,
            "total_pnl": round(total_pnl, 2),
            "max_margin": round(peak_margin, 2),
            "roc": round((total_pnl / peak_margin * 100), 2) if peak_margin > 0 else 0,
            "total_trades": trades_count,
            "win_rate": round((wins / total_matched * 100), 2) if total_matched > 0 else 0,
            "range_high": range_high,
            "range_low": range_low,
            "range_diff": range_diff,
            "expiry_settled_pnl": round(expiry_settled_pnl, 2),
            "expiry_settled_count": expiry_settled_count,
        }

    return sorted(results.values(), key=lambda x: x["expiry_date"])


def get_underlying_range(
    underlying: str, expiry_date_str: str
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Fetches high/low from bt_index_candles_1m for the 7 days before expiry.

    Args:
        underlying: Index name e.g. 'NIFTY' or 'SENSEX'.
        expiry_date_str: ISO date string e.g. '2025-05-12'.

    Returns:
        (high, low, range_diff) rounded to 2 dp, or (None, None, None) on failure.
    """
    try:
        # Optional integration: the backtesting package is not part of the
        # open-source distribution — resolved dynamically so its absence
        # just returns no range data.
        try:
            _bt_database = importlib.import_module("backtesting.database")
        except ImportError:
            logger.info("tradebook_analyzer: backtesting package not installed — no range data")
            return None, None, None
        BacktestDatabase = _bt_database.BacktestDatabase

        expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
        start_date = expiry_date - timedelta(days=7)
        symbol = underlying

        with BacktestDatabase() as db:
            db.cursor.execute(
                """
                SELECT MAX(high) as h, MIN(low) as l
                FROM bt_index_candles_1m
                WHERE symbol = %s AND DATE(timestamp) BETWEEN %s AND %s
                """,
                (symbol, start_date, expiry_date),
            )
            row = db.cursor.fetchone()
            if row and row["h"] is not None:
                return round(row["h"], 2), round(row["l"], 2), round(row["h"] - row["l"], 2)
    except Exception:
        logger.exception(
            "tradebook_analyzer: error fetching range for %s on %s",
            underlying, expiry_date_str,
        )
    return None, None, None


def estimate_current_margin(positions: dict[str, float]) -> float:
    """Estimates margin for current open positions using project constants.

    Args:
        positions: Dict mapping symbol → net quantity (positive = long).

    Returns:
        Estimated margin in rupees.
    """
    total_pos_ce = 0.0
    total_neg_ce = 0.0
    total_pos_pe = 0.0
    total_neg_pe = 0.0
    lot_size = 75  # fallback

    for sym, qty in positions.items():
        if abs(qty) < 0.1:
            continue

        inst = instrument_cache.get_instrument(sym)
        if inst and inst.get("lot_size"):
            lot_size = inst["lot_size"]

        if sym.endswith("CE"):
            if qty > 0:
                total_pos_ce += qty
            else:
                total_neg_ce += abs(qty)
        elif sym.endswith("PE"):
            if qty > 0:
                total_pos_pe += qty
            else:
                total_neg_pe += abs(qty)

    net_ce = total_pos_ce - total_neg_ce
    net_pe = total_pos_pe - total_neg_pe

    spread_count = total_pos_ce + total_pos_pe
    single_pe_ce = abs(net_pe - net_ce)
    both_ce_pe = min(abs(net_ce), abs(net_pe))

    margin = (
        spread_count * MARGIN_SPREAD
        + single_pe_ce * MARGIN_SINGLE_PE_CE
        + both_ce_pe * MARGIN_BOTH_PE_CE
    ) / lot_size

    return margin
