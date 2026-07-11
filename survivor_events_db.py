"""SQLite event log for Survivor algo instances.

Records every significant event (order placements, anchor changes, delta readings)
so that each instance's history is visible in the dashboard event log modal.
"""

from __future__ import annotations

import logging
import os
import sqlite3

_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "survivor_status", "survivor_events.db"
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    """Create the survivor_events table if it does not already exist."""
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS survivor_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                pid                 INTEGER NOT NULL,
                symbol_initials     TEXT    NOT NULL,
                index_type          TEXT    NOT NULL,
                event_type          TEXT    NOT NULL,
                event_time          TEXT    NOT NULL,
                spot_price          REAL,
                anchor_side         TEXT,
                old_anchor          REAL,
                new_anchor          REAL,
                ce_anchor           REAL,
                pe_anchor           REAL,
                order_symbol        TEXT,
                order_side          TEXT,
                order_price         REAL,
                order_quantity      INTEGER,
                delta_at_spot       REAL,
                delta_at_ce_anchor  REAL,
                delta_at_pe_anchor  REAL,
                notes               TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_se_pid  ON survivor_events(pid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_se_time ON survivor_events(event_time)")


def log_event(
    pid: int,
    symbol_initials: str,
    index_type: str,
    event_type: str,
    *,
    spot_price: float | None = None,
    anchor_side: str | None = None,
    old_anchor: float | None = None,
    new_anchor: float | None = None,
    ce_anchor: float | None = None,
    pe_anchor: float | None = None,
    order_symbol: str | None = None,
    order_side: str | None = None,
    order_price: float | None = None,
    order_quantity: int | None = None,
    delta_at_spot: float | None = None,
    delta_at_ce_anchor: float | None = None,
    delta_at_pe_anchor: float | None = None,
    notes: str | None = None,
) -> None:
    """Insert one event row. Exceptions are caught internally — never raises.

    Args:
        pid: OS process ID of the survivor instance.
        symbol_initials: Symbol prefix, e.g. "NIFTY26JUN".
        index_type: "NIFTY", "SENSEX", or "STOCK".
        event_type: One of INIT, ORDER_PLACED, PE_ANCHOR_SHIFT, CE_ANCHOR_SHIFT,
            PE_ANCHOR_REBALANCE, CE_ANCHOR_REBALANCE, RESET_GAP_TIGHTEN.
        spot_price: Live spot at the time of the event.
        anchor_side: "CE" or "PE" (for anchor-change events).
        old_anchor: Anchor value before the change.
        new_anchor: Anchor value after the change.
        ce_anchor: Snapshot of the CE anchor at event time.
        pe_anchor: Snapshot of the PE anchor at event time.
        order_symbol: Tradingsymbol of the placed order (ORDER_PLACED events).
        order_side: "CE" or "PE" for order events.
        order_price: Execution price (if known at log time).
        order_quantity: Shares placed in this order.
        delta_at_spot: Portfolio delta evaluated at current spot (rebalance events).
        delta_at_ce_anchor: Portfolio delta at hypothetical CE anchor spot.
        delta_at_pe_anchor: Portfolio delta at hypothetical PE anchor spot.
        notes: Free-form JSON string for supplementary info.
    """
    try:
        from common_lib import get_ist_now  # imported here to avoid circular import at module load

        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO survivor_events
                   (pid, symbol_initials, index_type, event_type, event_time,
                    spot_price, anchor_side, old_anchor, new_anchor,
                    ce_anchor, pe_anchor,
                    order_symbol, order_side, order_price, order_quantity,
                    delta_at_spot, delta_at_ce_anchor, delta_at_pe_anchor, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid,
                    symbol_initials,
                    index_type,
                    event_type,
                    get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
                    spot_price,
                    anchor_side,
                    old_anchor,
                    new_anchor,
                    ce_anchor,
                    pe_anchor,
                    order_symbol,
                    order_side,
                    order_price,
                    order_quantity,
                    delta_at_spot,
                    delta_at_ce_anchor,
                    delta_at_pe_anchor,
                    notes,
                ),
            )
    except Exception as exc:
        logging.error(f"survivor_events_db.log_event failed ({event_type}): {exc}")


def get_events_for_pid(pid: int, limit: int = 500) -> list[dict]:
    """Return events for one survivor instance, newest first.

    Args:
        pid: Process ID of the instance.
        limit: Maximum number of rows to return.

    Returns:
        List of event dicts, most recent first.
    """
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM survivor_events WHERE pid = ? ORDER BY id DESC LIMIT ?",
                (pid, limit),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logging.error(f"survivor_events_db.get_events_for_pid failed: {exc}")
        return []
