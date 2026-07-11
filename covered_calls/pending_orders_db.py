"""SQLite persistence for covered call pending orders.

Tracks orders placed from the dashboard so that a page refresh shows
"Pending Fill" instead of reverting to "Missing" before Kite positions
update. Orders are cleaned up automatically once Kite reports them as
COMPLETE, CANCELLED, or REJECTED.

DB file: covered_calls/pending_orders.db (created on first use).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_orders.db")

_DDL = """
CREATE TABLE IF NOT EXISTS pending_orders (
    order_id      TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    strike        REAL,
    expiry        TEXT,
    quantity      INTEGER,
    lots          INTEGER,
    lot_size      INTEGER,
    limit_price   REAL,
    placed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_po_symbol ON pending_orders(symbol);
"""


def _get_conn() -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection with dict-like row access."""
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    """Create the pending_orders table and index if they do not exist."""
    conn = _get_conn()
    try:
        conn.executescript(_DDL)
        conn.commit()
    except sqlite3.Error:
        logger.exception("pending_orders_db.init_db failed")
    finally:
        conn.close()


def save_pending_order(
    order_id: str,
    symbol: str,
    tradingsymbol: str,
    strike: float,
    expiry: str | None,
    quantity: int,
    lots: int,
    lot_size: int,
    limit_price: float,
    placed_at: str,
) -> None:
    """Persist a newly placed covered call order.

    Args:
        order_id: Kite order ID returned by place_covered_call().
        symbol: Underlying stock symbol, e.g. "TCS".
        tradingsymbol: NFO option tradingsymbol, e.g. "TCS25JUL3400CE".
        strike: Strike price.
        expiry: ISO date string of the option expiry.
        quantity: Number of shares (lots × lot_size).
        lots: Number of lots sold.
        lot_size: F&O lot size.
        limit_price: Limit price used for the order.
        placed_at: ISO datetime string when the order was placed.
    """
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_orders
                (order_id, symbol, tradingsymbol, strike, expiry,
                 quantity, lots, lot_size, limit_price, placed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, symbol, tradingsymbol, strike, expiry,
             quantity, lots, lot_size, limit_price, placed_at),
        )
        conn.commit()
        logger.info("Saved pending order %s for %s (%s)", order_id, symbol, tradingsymbol)
    except sqlite3.Error:
        logger.exception("save_pending_order failed for order_id=%s", order_id)
    finally:
        conn.close()


def get_pending_order(symbol: str) -> dict[str, Any] | None:
    """Return the most recently placed pending order for an underlying symbol.

    Args:
        symbol: Underlying stock symbol, e.g. "TCS".

    Returns:
        Dict with order fields, or None if no pending order exists.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT * FROM pending_orders
            WHERE symbol = ?
            ORDER BY placed_at DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        logger.exception("get_pending_order failed for symbol=%s", symbol)
        return None
    finally:
        conn.close()


def delete_pending_order(order_id: str) -> None:
    """Remove a pending order record once it is no longer in-flight.

    Args:
        order_id: Kite order ID to remove.
    """
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM pending_orders WHERE order_id = ?", (order_id,))
        conn.commit()
        logger.info("Deleted pending order %s", order_id)
    except sqlite3.Error:
        logger.exception("delete_pending_order failed for order_id=%s", order_id)
    finally:
        conn.close()


def get_all_pending_orders() -> list[dict[str, Any]]:
    """Return all pending orders, newest first.

    Returns:
        List of dicts, one per row, or empty list on error.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM pending_orders ORDER BY placed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        logger.exception("get_all_pending_orders failed")
        return []
    finally:
        conn.close()
