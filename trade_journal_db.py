"""
Trade Journal SQLite Cache.

Caches FIFO-paired round trips in a local SQLite database so the expensive
pair_trades() computation runs at most once per day (or when new JSON order
files are detected). All trade journal API requests query the cache instead of
re-pairing from scratch.

Rebuild triggers:
  1. Auto-detect: any JSON order file mtime > last_sync_ts
  2. Daily: today's date > last_sync_date in DB
  3. Manual: force_rebuild() called explicitly (post-Zerodha reconcile or button)
"""

import calendar
import glob
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "trade_journal.db")

_REBUILD_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS round_trips (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT    NOT NULL,
    direction               TEXT    NOT NULL,
    matched_qty             INTEGER NOT NULL,
    pnl                     REAL    NOT NULL,
    option_type             TEXT,
    expiry                  TEXT,
    instrument_token        INTEGER,
    segment                 TEXT,
    open_timestamp          TEXT    NOT NULL,
    open_transaction_type   TEXT    NOT NULL,
    open_price              REAL    NOT NULL,
    open_quantity           INTEGER NOT NULL,
    open_algo_source        TEXT,
    open_trade_date         TEXT    NOT NULL,
    close_timestamp         TEXT    NOT NULL,
    close_transaction_type  TEXT    NOT NULL,
    close_price             REAL    NOT NULL,
    close_quantity          INTEGER NOT NULL,
    close_algo_source       TEXT,
    close_trade_date        TEXT    NOT NULL,
    attr_algo_pnl           REAL    NOT NULL DEFAULT 0.0,
    attr_manual_pnl         REAL    NOT NULL DEFAULT 0.0,
    attr_label              TEXT,
    open_algo_name          TEXT,
    open_algo_category      TEXT,
    open_is_algo            INTEGER NOT NULL DEFAULT 0,
    close_algo_name         TEXT,
    close_algo_category     TEXT,
    close_is_algo           INTEGER NOT NULL DEFAULT 0,
    is_expiry_close         INTEGER NOT NULL DEFAULT 0,
    open_order_id           TEXT,
    close_order_id          TEXT
);

CREATE INDEX IF NOT EXISTS idx_rt_open_date  ON round_trips(open_trade_date);
CREATE INDEX IF NOT EXISTS idx_rt_close_date ON round_trips(close_trade_date);

CREATE TABLE IF NOT EXISTS unpaired_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date       TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    price            REAL NOT NULL,
    quantity         INTEGER NOT NULL,
    algo_source      TEXT,
    raw_json         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_unpaired_date ON unpaired_orders(trade_date);

CREATE TABLE IF NOT EXISTS sync_metadata (
    id                  INTEGER PRIMARY KEY CHECK(id = 1),
    last_sync_ts        REAL    NOT NULL,
    last_sync_date      TEXT    NOT NULL,
    round_trip_count    INTEGER NOT NULL DEFAULT 0,
    unpaired_count      INTEGER NOT NULL DEFAULT 0,
    source_file_count   INTEGER NOT NULL DEFAULT 0
);
"""

_INSERT_ROUND_TRIP = """
INSERT INTO round_trips (
    symbol, direction, matched_qty, pnl, option_type, expiry,
    instrument_token, segment,
    open_timestamp, open_transaction_type, open_price, open_quantity,
    open_algo_source, open_trade_date,
    close_timestamp, close_transaction_type, close_price, close_quantity,
    close_algo_source, close_trade_date,
    attr_algo_pnl, attr_manual_pnl, attr_label,
    open_algo_name, open_algo_category, open_is_algo,
    close_algo_name, close_algo_category, close_is_algo,
    is_expiry_close, open_order_id, close_order_id
) VALUES (
    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
)
"""

_INSERT_UNPAIRED = """
INSERT INTO unpaired_orders
    (trade_date, timestamp, symbol, transaction_type, price, quantity,
     algo_source, raw_json)
VALUES (?,?,?,?,?,?,?,?)
"""


# ---------------------------------------------------------------------------
# Expiry / settlement constants
# ---------------------------------------------------------------------------

# Zerodha weekly option symbol: YY + month_char + DD (5 chars total)
_WEEKLY_MONTH_MAP: dict[str, int] = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "6": 6, "7": 7, "8": 8, "9": 9,
    "O": 10, "N": 11, "D": 12,
}

# Zerodha monthly option symbol: YY + MMM (5 chars total)
_MONTHLY_ABBREV_MAP: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_IST = timezone(timedelta(hours=5, minutes=30))

# Underlyings whose monthly options expire on the last Tuesday (NSE changed NIFTY from Thursday)
_TUESDAY_EXPIRY_UNDERLYINGS: frozenset[str] = frozenset({"NIFTY"})

# Maps option underlying prefix → Kite quote symbol (used as fallback for kite historical API)
_SPOT_SYMBOL_MAP: dict[str, str] = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
}



# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create tables and indexes if they do not exist.

    Safe to call multiple times (all DDL uses IF NOT EXISTS). Also runs
    schema migrations to add columns to existing DBs.
    """
    conn = get_db_connection()
    try:
        conn.executescript(_DDL)
        conn.commit()
        # Idempotent migrations for columns added after initial schema
        migrations = [
            "ALTER TABLE round_trips ADD COLUMN is_expiry_close INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE round_trips ADD COLUMN open_order_id TEXT",
            "ALTER TABLE round_trips ADD COLUMN close_order_id TEXT",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists
    finally:
        conn.close()


def get_db_connection() -> sqlite3.Connection:
    """Return a new SQLite connection with sensible defaults.

    Returns:
        sqlite3.Connection with row_factory, WAL journal mode, and
        a 5-second busy timeout for lock contention.

    Note:
        Caller is responsible for closing the connection.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


def _get_status_dir() -> str:
    """Return the path to the directory containing executed_orders_*.json files."""
    import trade_journal as tj  # lazy to avoid circular import

    return tj._get_status_dir()


def needs_rebuild() -> bool:
    """Check whether the SQLite cache is stale and needs a full rebuild.

    Returns True when any of the following hold:
      1. DB file does not exist.
      2. sync_metadata has no row (never built).
      3. today's date > last_sync_date (daily invalidation).
      4. Any order JSON file has mtime > last_sync_ts.

    Returns:
        bool: True if rebuild required, False if cache is fresh.
    """
    if not os.path.exists(DB_PATH):
        return True

    try:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT last_sync_ts, last_sync_date FROM sync_metadata WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return True

    if row is None:
        return True

    last_sync_ts: float = row["last_sync_ts"]
    last_sync_date: str = row["last_sync_date"]

    if date.today().isoformat() > last_sync_date:
        return True

    try:
        status_dir = _get_status_dir()
        json_files = glob.glob(os.path.join(status_dir, "executed_orders_*.json"))
        if not json_files:
            return False
        max_mtime: float = max(os.path.getmtime(p) for p in json_files)
        return max_mtime > last_sync_ts
    except OSError as exc:
        logger.warning("Could not stat order files during staleness check: %s", exc)
        return True


# ---------------------------------------------------------------------------
# Row conversion
# ---------------------------------------------------------------------------


def _round_trip_to_row(rt: dict[str, Any]) -> tuple:
    """Flatten a round_trip dict to a tuple for DB insertion.

    Args:
        rt: Enriched round trip dict (attribution key must be present).

    Returns:
        Tuple of 32 values matching _INSERT_ROUND_TRIP column order.
    """
    attr = rt.get("attribution") or {}
    open_t = rt["open_trade"]
    close_t = rt["close_trade"]
    open_algo = attr.get("open_algo_info") or {}
    close_algo = attr.get("close_algo_info") or {}

    return (
        rt["symbol"],
        rt["direction"],
        rt["matched_qty"],
        rt["pnl"],
        rt.get("option_type"),
        rt.get("expiry"),
        rt.get("instrument_token"),
        rt.get("segment"),
        # open_trade
        open_t.get("timestamp"),
        open_t.get("transaction_type"),
        float(open_t.get("price", 0)),
        int(open_t.get("quantity", 0)),
        open_t.get("algo_source"),
        open_t.get("trade_date"),
        # close_trade
        close_t.get("timestamp"),
        close_t.get("transaction_type"),
        float(close_t.get("price", 0)),
        int(close_t.get("quantity", 0)),
        close_t.get("algo_source"),
        close_t.get("trade_date"),
        # attribution
        float(attr.get("algo_pnl", 0.0)),
        float(attr.get("manual_pnl", 0.0)),
        attr.get("attribution_label"),
        open_algo.get("algo_name"),
        open_algo.get("category"),
        int(bool(open_algo.get("is_algo", False))),
        close_algo.get("algo_name"),
        close_algo.get("category"),
        int(bool(close_algo.get("is_algo", False))),
        int(bool(rt.get("is_expiry_close", False))),
        # order IDs
        open_t.get("order_id", "") or "",
        close_t.get("order_id", "") or "",
    )


def _row_to_round_trip(row: sqlite3.Row) -> dict[str, Any]:
    """Reconstruct a nested round_trip dict from a flat DB row.

    Args:
        row: sqlite3.Row from the round_trips table.

    Returns:
        Dict matching the round_trip structure callers expect.
    """
    return {
        "symbol": row["symbol"],
        "direction": row["direction"],
        "matched_qty": row["matched_qty"],
        "pnl": row["pnl"],
        "option_type": row["option_type"],
        "expiry": row["expiry"],
        "instrument_token": row["instrument_token"],
        "segment": row["segment"],
        "open_trade": {
            "timestamp": row["open_timestamp"],
            "transaction_type": row["open_transaction_type"],
            "price": row["open_price"],
            "quantity": row["open_quantity"],
            "algo_source": row["open_algo_source"],
            "trade_date": row["open_trade_date"],
            "order_id": row["open_order_id"] or "",
        },
        "close_trade": {
            "timestamp": row["close_timestamp"],
            "transaction_type": row["close_transaction_type"],
            "price": row["close_price"],
            "quantity": row["close_quantity"],
            "algo_source": row["close_algo_source"],
            "trade_date": row["close_trade_date"],
            "order_id": row["close_order_id"] or "",
        },
        "attribution": {
            "algo_pnl": row["attr_algo_pnl"],
            "manual_pnl": row["attr_manual_pnl"],
            "attribution_label": row["attr_label"],
            "open_algo_info": {
                "algo_name": row["open_algo_name"],
                "category": row["open_algo_category"],
                "is_algo": bool(row["open_is_algo"]),
            },
            "close_algo_info": {
                "algo_name": row["close_algo_name"],
                "category": row["close_algo_category"],
                "is_algo": bool(row["close_is_algo"]),
            },
        },
        "is_expiry_close": bool(row["is_expiry_close"]),
    }


def _unpaired_to_row(order: dict[str, Any]) -> tuple:
    """Flatten an unpaired order dict for DB insertion."""
    return (
        order.get("trade_date", ""),
        order.get("timestamp", ""),
        order.get("symbol", ""),
        order.get("transaction_type", ""),
        float(order.get("price", 0)),
        int(order.get("quantity", 0)),
        order.get("algo_source"),
        json.dumps(order),
    )


# ---------------------------------------------------------------------------
# Expiry settlement helpers
# ---------------------------------------------------------------------------


def _last_thursday_of_month(year: int, month: int) -> date:
    """Return the last Thursday of a given month (NSE monthly expiry convention).

    Args:
        year: Full year e.g. 2024.
        month: Month number 1–12.

    Returns:
        Date of the last Thursday in that month.
    """
    last_day = calendar.monthrange(year, month)[1]
    candidate = date(year, month, last_day)
    while candidate.weekday() != 3:  # Thursday = 3
        candidate -= timedelta(days=1)
    return candidate


def _last_tuesday_of_month(year: int, month: int) -> date:
    """Return the last Tuesday of a given month (NSE NIFTY monthly expiry convention).

    Args:
        year: Full year e.g. 2026.
        month: Month number 1–12.

    Returns:
        Date of the last Tuesday in that month.
    """
    last_day = calendar.monthrange(year, month)[1]
    candidate = date(year, month, last_day)
    while candidate.weekday() != 1:  # Tuesday = 1
        candidate -= timedelta(days=1)
    return candidate


def _parse_expiry_to_date(expiry_str: str, underlying_name: str = "") -> Optional[date]:
    """Decode a Zerodha-encoded expiry string to a Python date.

    Handles two 5-character formats:
    - Weekly: '26217' → date(2026, 2, 17)  (YY + month_char + DD)
    - Monthly: '24JAN' → last expiry weekday of January 2024
      NIFTY: last Tuesday (NSE changed from Thursday to Tuesday)
      All others: last Thursday (default NSE/BSE convention)

    Args:
        expiry_str: Encoded expiry from order JSON (e.g. '26217', '24JAN').
        underlying_name: Underlying index name (e.g. 'NIFTY', 'SENSEX').

    Returns:
        Decoded date, or None if the string is unrecognisable.
    """
    if not expiry_str or len(expiry_str) != 5:
        return None

    try:
        year = 2000 + int(expiry_str[:2])
    except ValueError:
        return None

    month_part = expiry_str[2:]

    # Monthly format: last 3 chars are alpha (e.g. 'JAN')
    if month_part.isalpha():
        month = _MONTHLY_ABBREV_MAP.get(month_part.upper())
        if month is None:
            return None
        if underlying_name.upper() in _TUESDAY_EXPIRY_UNDERLYINGS:
            return _last_tuesday_of_month(year, month)
        return _last_thursday_of_month(year, month)

    # Weekly format: single month char + 2-digit day (e.g. '217' → Feb 17)
    month_char = expiry_str[2]
    day_str = expiry_str[3:]
    month = _WEEKLY_MONTH_MAP.get(month_char)
    if month is None:
        return None
    try:
        return date(year, month, int(day_str))
    except ValueError:
        return None


def _get_underlying_name(symbol: str) -> str:
    """Extract the underlying name from a trading symbol.

    Reads all leading uppercase letters before the first digit.

    Args:
        symbol: Trading symbol e.g. 'NIFTY2621725550PE', 'BANKNIFTY24JANFUT'.

    Returns:
        Underlying name e.g. 'NIFTY', 'BANKNIFTY', 'SENSEX'.
    """
    match = re.match(r"^([A-Z]+)", symbol)
    return match.group(1) if match else ""


def _parse_strike_from_symbol(symbol: str, expiry_code: str) -> Optional[float]:
    """Extract the strike price from an options trading symbol.

    Strips the underlying prefix and expiry code, then reads the numeric
    digits immediately before the CE/PE suffix.

    Args:
        symbol: Full trading symbol e.g. 'NIFTY2621725550PE'.
        expiry_code: Expiry code stored on the order e.g. '26217'.

    Returns:
        Strike price as float, or None if parsing fails.
    """
    underlying = _get_underlying_name(symbol)
    if not underlying or not expiry_code:
        return None

    after_underlying = symbol[len(underlying):]          # '2621725550PE'
    exp_pos = after_underlying.find(expiry_code)
    if exp_pos == -1:
        return None

    after_expiry = after_underlying[exp_pos + len(expiry_code):]  # '25550PE'
    strike_str = re.sub(r"(CE|PE)$", "", after_expiry)            # '25550'
    try:
        return float(strike_str) if strike_str else None
    except ValueError:
        return None



_KITE_AUTH_ERROR_KEYWORDS = ("invalid", "incorrect", "token", "access_token", "api_key", "expired", "403")

# Sentinel key stored in price_cache to signal that Kite auth failed this run.
# All subsequent calls to _get_underlying_close will return None immediately.
_PRICE_CACHE_KITE_AUTH_FAILED = "_kite_auth_failed"


def _get_underlying_close_kite(underlying_name: str, expiry_date: date) -> Optional[float]:
    """Fetch underlying index closing price via Zerodha historical API (requires valid auth).

    Args:
        underlying_name: e.g. 'NIFTY', 'SENSEX', 'BANKNIFTY'.
        expiry_date: The date whose closing price is needed.

    Returns:
        Closing price as float, or None if the data is unavailable for that date.

    Raises:
        PermissionError: If the Kite session is invalid (bad token / expired). The caller
            should treat this as a permanent failure for the rest of the backfill run and
            skip all further Kite calls.
    """
    spot_kite_symbol = _SPOT_SYMBOL_MAP.get(underlying_name)
    if not spot_kite_symbol:
        return None

    try:
        import common_lib as cl  # lazy import; already loaded in Flask context
        quote = cl.kite.quote([spot_kite_symbol])
        token = quote[spot_kite_symbol]["instrument_token"]
        from_dt = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 9, 15, tzinfo=_IST)
        to_dt   = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 15, 30, tzinfo=_IST)
        candles = cl.kite.historical_data(token, from_dt, to_dt, "day")
        return float(candles[-1]["close"]) if candles else None
    except Exception as exc:
        error_text = str(exc).lower()
        if any(keyword in error_text for keyword in _KITE_AUTH_ERROR_KEYWORDS):
            logger.warning(
                "trade_journal_db: kite auth invalid for %s on %s — will skip kite for this run: %s",
                underlying_name, expiry_date.isoformat(), exc,
            )
            raise PermissionError(str(exc)) from exc
        logger.exception(
            "trade_journal_db: kite fetch failed for %s close on %s",
            underlying_name, expiry_date.isoformat(),
        )
        return None


def _get_underlying_close(
    underlying_name: str,
    expiry_date: date,
    price_cache: dict[str, Optional[float]],
) -> Optional[float]:
    """Fetch the underlying index closing price on expiry day via Zerodha Kite.

    On auth failure the sentinel _PRICE_CACHE_KITE_AUTH_FAILED is set in
    price_cache and all subsequent calls for this run return None immediately
    without hitting the API again.  For holidays where Kite returns no data,
    up to 3 prior calendar days are tried.

    Args:
        underlying_name: e.g. 'NIFTY', 'SENSEX', 'BANKNIFTY'.
        expiry_date: The date whose closing price is needed.
        price_cache: Mutable dict for in-run caching and auth-failure flag.

    Returns:
        Closing price as float, or None if Kite auth is invalid or data unavailable.
    """
    cache_key = f"{underlying_name}:{expiry_date.isoformat()}"
    if cache_key in price_cache:
        return price_cache[cache_key]

    if price_cache.get(_PRICE_CACHE_KITE_AUTH_FAILED):
        return None

    closing_price: Optional[float] = None
    try:
        closing_price = _get_underlying_close_kite(underlying_name, expiry_date)
    except PermissionError:
        price_cache[_PRICE_CACHE_KITE_AUTH_FAILED] = True
        return None

    if closing_price is None:
        logger.warning(
            "trade_journal_db: kite returned no data for %s on %s; trying holiday fallback",
            underlying_name, expiry_date.isoformat(),
        )
        for offset in range(1, 4):
            fallback_date = expiry_date - timedelta(days=offset)
            try:
                closing_price = _get_underlying_close_kite(underlying_name, fallback_date)
            except PermissionError:
                price_cache[_PRICE_CACHE_KITE_AUTH_FAILED] = True
                return None
            if closing_price is not None:
                logger.info(
                    "trade_journal_db: holiday fallback — used %s close from %s (nominal expiry %s)",
                    underlying_name, fallback_date.isoformat(), expiry_date.isoformat(),
                )
                break

    if closing_price is None:
        logger.warning(
            "trade_journal_db: kite data unavailable for %s on %s; settlement will use 0.0 fallback",
            underlying_name, expiry_date.isoformat(),
        )

    price_cache[cache_key] = closing_price
    return closing_price


def _make_expiry_round_trip(
    order: dict[str, Any],
    expiry_date: date,
    settlement_price: float,
) -> dict[str, Any]:
    """Create a synthetic round trip for an option or future that expired.

    The synthetic closing trade is timestamped at 15:30 IST on expiry day
    with price equal to the settlement value.

    Args:
        order: Original unpaired order dict from executed_orders JSON files.
        expiry_date: The date the instrument expired.
        settlement_price: Settlement price at expiry (≥ 0 for options; any value for futures).

    Returns:
        Round trip dict in the nested format from pair_trades(), with
        is_expiry_close=True.
    """
    original_tx = order.get("transaction_type", "BUY")
    reverse_tx = "BUY" if original_tx == "SELL" else "SELL"
    expiry_ts = f"{expiry_date.isoformat()}T15:30:00+05:30"
    qty = int(order.get("quantity", 0))
    open_price = float(order.get("price", 0))

    if original_tx == "SELL":
        direction = "sell_first"
        sell_price, buy_price = open_price, settlement_price
    else:
        direction = "buy_first"
        sell_price, buy_price = settlement_price, open_price

    return {
        "symbol": order.get("symbol", ""),
        "direction": direction,
        "matched_qty": qty,
        "pnl": round((sell_price - buy_price) * qty, 2),
        "option_type": order.get("option_type", ""),
        "expiry": order.get("expiry", ""),
        "is_expiry_close": True,
        "open_trade": {
            "timestamp": order.get("timestamp"),
            "transaction_type": original_tx,
            "price": open_price,
            "quantity": qty,
            "algo_source": order.get("algo_source", "Unknown"),
            "trade_date": order.get("trade_date", ""),
            "order_id": order.get("order_id", "") or "",
        },
        "close_trade": {
            "timestamp": expiry_ts,
            "transaction_type": reverse_tx,
            "price": settlement_price,
            "quantity": qty,
            "algo_source": "Expiry",
            "trade_date": expiry_date.isoformat(),
            "order_id": "",  # synthetic expiry close has no Zerodha order ID
        },
    }


def _classify_unpaired_orders(
    unpaired: list[dict[str, Any]],
    today: date,
    price_cache: dict[str, Optional[float]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split unpaired orders into expired (synthetic round trips) and still-active.

    For each expired CE/PE/FUT order, fetches the underlying closing price via
    Zerodha, computes the settlement value, and creates a synthetic closing trade.
    Orders whose expiry cannot be determined are kept as unpaired (fail-safe).

    Args:
        unpaired: List of unpaired order dicts from pair_trades().
        today: Reference date; orders with expiry < today are treated as expired.
        price_cache: Shared dict for caching underlying close prices to avoid
            duplicate API calls within a rebuild run.

    Returns:
        Tuple of (synthetic_round_trips, still_active_unpaired).
    """
    import instrument_cache as ic

    synthetic_round_trips: list[dict[str, Any]] = []
    active_unpaired: list[dict[str, Any]] = []

    for order in unpaired:
        option_type = order.get("option_type", "")
        if option_type not in ("CE", "PE", "FUT"):
            active_unpaired.append(order)
            continue

        symbol = order.get("symbol", "")
        underlying_name = _get_underlying_name(symbol)

        # Resolve expiry date: instrument cache first, then symbol parse fallback
        inst = ic.get_instrument(symbol)
        expiry_date: Optional[date] = None
        if inst and inst.get("expiry"):
            try:
                expiry_date = date.fromisoformat(str(inst["expiry"]))
            except ValueError:
                pass
        if expiry_date is None:
            expiry_date = _parse_expiry_to_date(order.get("expiry", ""), underlying_name)

        if expiry_date is None or expiry_date >= today:
            active_unpaired.append(order)
            continue

        # Expired — compute settlement price
        underlying_close = _get_underlying_close(underlying_name, expiry_date, price_cache)
        fallback_close = underlying_close if underlying_close is not None else 0.0

        if option_type == "FUT":
            settlement_price = fallback_close
        else:
            strike: Optional[float] = inst.get("strike") if inst else None
            if strike is None:
                strike = _parse_strike_from_symbol(symbol, order.get("expiry", ""))
            if strike is None:
                logger.warning(
                    "trade_journal_db: cannot determine strike for %s; keeping unpaired", symbol
                )
                active_unpaired.append(order)
                continue

            if option_type == "CE":
                settlement_price = max(0.0, fallback_close - strike)
            else:
                settlement_price = max(0.0, strike - fallback_close)

        synthetic_round_trips.append(
            _make_expiry_round_trip(order, expiry_date, settlement_price)
        )
        logger.info(
            "trade_journal_db: expired %s %s → settlement %.2f (underlying close %.2f)",
            option_type, symbol, settlement_price, fallback_close,
        )

    return synthetic_round_trips, active_unpaired


# ---------------------------------------------------------------------------
# Cache rebuild
# ---------------------------------------------------------------------------


def _build_cache() -> dict[str, int]:
    """Rebuild the cache from JSON order files.

    Loads all available order JSON files, runs FIFO pairing via pair_trades(),
    enriches each round trip with attribution and instrument metadata, then
    writes everything to SQLite in a single atomic transaction.

    Returns:
        dict with keys: round_trip_count, unpaired_count,
        source_file_count, duration_ms.
    """
    import trade_journal as tj
    import instrument_cache as ic

    t0 = time.monotonic()
    logger.info("trade_journal_db: starting cache rebuild")

    available_dates = tj.get_available_dates()
    if not available_dates:
        logger.warning("trade_journal_db: no order files found; cache will be empty")
        _write_empty_cache()
        return {"round_trip_count": 0, "unpaired_count": 0,
                "source_file_count": 0, "duration_ms": 0}

    start_date = date.fromisoformat(available_dates[0])
    end_date = date.fromisoformat(available_dates[-1])

    orders = tj.load_orders_range(start_date, end_date)

    # Second-chance GTT attribution: if record_order_complete() couldn't resolve
    # a GTT tag at fire time (e.g. DB unavailable), try again during rebuild.
    for order in orders:
        src = order.get('algo_source', '')
        if src and (src.startswith('GTT_S_') or src.startswith('GTT_B_')):
            resolved = ic.get_gtt_algo_source(src)
            if resolved:
                order['algo_source'] = resolved

    round_trips_raw, unpaired_raw = tj.pair_trades(orders)

    # Classify unpaired: auto-close expired instruments at settlement price
    price_cache: dict[str, Optional[float]] = {}
    synthetic_rts, active_unpaired = _classify_unpaired_orders(
        unpaired_raw, date.today(), price_cache
    )
    all_rts = round_trips_raw + synthetic_rts

    enriched: list[dict] = []
    for rt in all_rts:
        inst = ic.get_instrument(rt["symbol"])
        rt["instrument_token"] = inst.get("instrument_token") if inst else None
        rt["segment"] = inst.get("segment") if inst else None
        rt["attribution"] = tj.attribute_pnl(rt)
        enriched.append(rt)

    round_trip_rows = [_round_trip_to_row(rt) for rt in enriched]
    unpaired_rows = [_unpaired_to_row(o) for o in active_unpaired]
    sync_ts = time.time()

    conn = get_db_connection()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM round_trips")
        conn.execute("DELETE FROM unpaired_orders")
        if round_trip_rows:
            conn.executemany(_INSERT_ROUND_TRIP, round_trip_rows)
        if unpaired_rows:
            conn.executemany(_INSERT_UNPAIRED, unpaired_rows)
        conn.execute(
            """INSERT OR REPLACE INTO sync_metadata
               (id, last_sync_ts, last_sync_date,
                round_trip_count, unpaired_count, source_file_count)
               VALUES (1, ?, ?, ?, ?, ?)""",
            (sync_ts, date.today().isoformat(),
             len(enriched), len(active_unpaired), len(available_dates)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "trade_journal_db: rebuilt %d round trips (%d expiry-closed), %d unpaired "
        "from %d files in %dms",
        len(enriched), len(synthetic_rts), len(active_unpaired),
        len(available_dates), duration_ms,
    )
    return {
        "round_trip_count": len(enriched),
        "unpaired_count": len(active_unpaired),
        "source_file_count": len(available_dates),
        "duration_ms": duration_ms,
    }


def _write_empty_cache() -> None:
    """Write an empty cache with current sync timestamp (no order files case)."""
    conn = get_db_connection()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM round_trips")
        conn.execute("DELETE FROM unpaired_orders")
        conn.execute(
            """INSERT OR REPLACE INTO sync_metadata
               (id, last_sync_ts, last_sync_date,
                round_trip_count, unpaired_count, source_file_count)
               VALUES (1, ?, ?, 0, 0, 0)""",
            (time.time(), date.today().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def rebuild_cache() -> dict[str, int]:
    """Rebuild the cache if stale; no-op if already fresh.

    Thread-safe: uses double-check locking so only one thread rebuilds
    even if multiple Flask threads detect staleness simultaneously.

    Returns:
        Stats dict from _build_cache(), or empty dict if cache was fresh.
    """
    if needs_rebuild():
        with _REBUILD_LOCK:
            if needs_rebuild():
                return _build_cache()
    return {}


def force_rebuild() -> dict[str, int]:
    """Force a full cache rebuild regardless of staleness.

    Used after Zerodha reconcile completes or on explicit user request.

    Returns:
        Stats dict: round_trip_count, unpaired_count, source_file_count,
        duration_ms.
    """
    with _REBUILD_LOCK:
        return _build_cache()


def ensure_cache_fresh() -> None:
    """Ensure the cache is current; rebuild silently if stale.

    Fast path: reads one DB row + globs JSON file mtimes (~5ms if fresh).
    Slow path: acquires lock and runs full rebuild (~1-3s typical).

    Called at the top of every trade log API request.
    """
    if needs_rebuild():
        with _REBUILD_LOCK:
            if needs_rebuild():
                _build_cache()


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------


def query_round_trips(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Fetch paired round trips for a date range, newest first.

    Filters on close_trade_date BETWEEN start_date AND end_date so that
    cross-day trades (opened one day, closed another) appear on the day
    they were COMPLETED rather than the day they were opened. Results are
    ordered by close_timestamp DESC to show the most recent trades first.

    Args:
        start_date: YYYY-MM-DD string (inclusive).
        end_date: YYYY-MM-DD string (inclusive).

    Returns:
        List of round_trip dicts in the nested format callers expect.
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """SELECT * FROM round_trips
               WHERE close_trade_date BETWEEN ? AND ?
               ORDER BY close_trade_date DESC, close_timestamp DESC""",
            (start_date, end_date),
        )
        return [_row_to_round_trip(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def query_unpaired(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Fetch unpaired orders for a date range.

    Args:
        start_date: YYYY-MM-DD string (inclusive).
        end_date: YYYY-MM-DD string (inclusive).

    Returns:
        List of original order dicts (deserialized from stored raw_json).
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT raw_json FROM unpaired_orders WHERE trade_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        return [json.loads(row["raw_json"]) for row in cursor.fetchall()]
    finally:
        conn.close()


def query_unpaired_all() -> list[dict[str, Any]]:
    """Fetch all unpaired orders across every date in the cache.

    Used by position validation to compute the journal's implied open positions
    without a date-range filter.

    Returns:
        List of original order dicts (deserialized from stored raw_json).
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute("SELECT raw_json FROM unpaired_orders")
        return [json.loads(row["raw_json"]) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_sync_metadata() -> dict[str, Any]:
    """Return the current sync metadata row, or an empty dict if not built yet.

    Returns:
        dict with last_sync_ts, last_sync_date, round_trip_count,
        unpaired_count, source_file_count.
    """
    if not os.path.exists(DB_PATH):
        return {}
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sync_metadata WHERE id = 1"
        ).fetchone()
        if row is None:
            return {}
        return dict(row)
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Module init
# ---------------------------------------------------------------------------

init_db()
