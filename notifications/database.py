"""Notifications database — SQLite schema and CRUD helpers.

Uses the same instruments.db file as instrument_cache.py so there is only one
SQLite file to manage. Tables are created with CREATE TABLE IF NOT EXISTS so
init_db() is safe to call multiple times.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "instruments.db")

_VALID_STATUSES = {"active", "completed", "ignored"}


def _get_connection() -> sqlite3.Connection:
    """Return a sqlite3 connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_ist() -> str:
    """Return the current IST time as an ISO-8601 string."""
    return datetime.now(_IST).isoformat()


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a SQLite table using PRAGMA table_info.

    Args:
        conn: Active SQLite connection.
        table_name: Name of the table to inspect.
        column_name: Name of the column to look for.

    Returns:
        True if the column exists, False otherwise.
    """
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def init_db() -> None:
    """Create the notifications and push_subscriptions tables if they don't exist.

    Also runs any pending schema migrations (e.g. adding the status column).
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    """
    with _get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notifications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                type       TEXT    NOT NULL,
                title      TEXT    NOT NULL,
                body       TEXT    NOT NULL,
                severity   TEXT    NOT NULL DEFAULT 'info',
                channels   TEXT    NOT NULL,
                metadata   TEXT,
                is_read    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL,
                sent_at    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_notifications_read
                ON notifications (is_read);
            CREATE INDEX IF NOT EXISTS idx_notifications_created
                ON notifications (created_at);

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint   TEXT NOT NULL UNIQUE,
                p256dh     TEXT NOT NULL,
                auth       TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alert_cooldowns (
                alert_key  TEXT PRIMARY KEY,
                last_fired TEXT NOT NULL
            );
        """)

        if not _column_exists(conn, "notifications", "status"):
            conn.execute(
                "ALTER TABLE notifications ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications (status)"
            )
            logger.info("Migrated notifications table: added status column")

    logger.info("Notifications DB tables initialised at %s", DB_PATH)


def save_notification(
    notification_type: str,
    title: str,
    body: str,
    severity: str,
    channels: list[str],
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """Persist a new notification row and return its row id.

    Args:
        notification_type: One of KITE_LOGIN_CHECK, COPY_ACCOUNT_ERROR,
            EARLY_EXIT_REQUIRED, etc.
        title: Short heading shown in the toast / push notification.
        body: Full notification message.
        severity: 'info', 'warning', or 'error'.
        channels: List of channels to attempt, e.g. ['telegram', 'in_app'].
        metadata: Optional extra context serialised as JSON.

    Returns:
        The auto-incremented row id of the inserted notification.
    """
    with _get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO notifications
                (type, title, body, severity, channels, metadata, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                notification_type,
                title,
                body,
                severity,
                json.dumps(channels),
                json.dumps(metadata) if metadata else None,
                _now_ist(),
            ),
        )
        return cursor.lastrowid  # type: ignore[return-value]


def mark_sent(notification_id: int) -> None:
    """Set sent_at to the current IST time for the given notification.

    Args:
        notification_id: Row id of the notification to update.
    """
    with _get_connection() as conn:
        conn.execute(
            "UPDATE notifications SET sent_at = ? WHERE id = ?",
            (_now_ist(), notification_id),
        )


def get_unread_notifications(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to *limit* unread notifications, newest first.

    Used exclusively by the toast poller — is_read=0 means the toast hasn't
    been shown yet. This is separate from the active/inactive lifecycle.

    Args:
        limit: Maximum number of rows to return.

    Returns:
        List of dicts with keys: id, type, title, body, severity, channels,
        metadata, created_at.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, type, title, body, severity, channels, metadata, created_at
            FROM notifications
            WHERE is_read = 0
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return _deserialise_rows(rows)


def get_active_notifications(limit: int = 100) -> list[dict[str, Any]]:
    """Return up to *limit* active notifications, newest first.

    'Active' means status='active', regardless of whether the toast has been
    shown (is_read). Used by the bell icon badge and dropdown.

    Args:
        limit: Maximum number of rows to return.

    Returns:
        List of dicts with keys: id, type, title, body, severity, channels,
        metadata, created_at, status.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, type, title, body, severity, channels, metadata, created_at, status
            FROM notifications
            WHERE status = 'active'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return _deserialise_rows(rows)


def get_all_notifications(
    active_limit: int = 200,
    inactive_limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    """Return notifications split into active and inactive sections.

    Used by the full notifications page. Active are status='active', inactive
    are status IN ('completed', 'ignored'), both sorted newest first.

    Args:
        active_limit: Max rows for the active section.
        inactive_limit: Max rows for the completed/ignored section.

    Returns:
        Dict with keys 'active' and 'inactive', each a list of notification dicts.
    """
    with _get_connection() as conn:
        active_rows = conn.execute(
            """
            SELECT id, type, title, body, severity, channels, metadata, created_at, status
            FROM notifications
            WHERE status = 'active'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (active_limit,),
        ).fetchall()

        inactive_rows = conn.execute(
            """
            SELECT id, type, title, body, severity, channels, metadata, created_at, status
            FROM notifications
            WHERE status IN ('completed', 'ignored')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (inactive_limit,),
        ).fetchall()

    return {
        "active": _deserialise_rows(active_rows),
        "inactive": _deserialise_rows(inactive_rows),
    }


def update_notification_status(notification_id: int, status: str) -> None:
    """Set the lifecycle status of a notification.

    Args:
        notification_id: Row id of the notification to update.
        status: Must be 'completed' or 'ignored'.

    Raises:
        ValueError: If status is not one of the allowed non-active values.
    """
    allowed = {"completed", "ignored"}
    if status not in allowed:
        raise ValueError(f"Invalid status '{status}'. Must be one of {allowed}.")

    with _get_connection() as conn:
        conn.execute(
            "UPDATE notifications SET status = ? WHERE id = ?",
            (status, notification_id),
        )
    logger.debug("Notification %d status set to '%s'", notification_id, status)


def mark_all_ignored(notification_types: Optional[list[str]] = None) -> int:
    """Set status='ignored' on all currently active notifications.

    Args:
        notification_types: If provided, only ignore notifications whose type
            is in this list. If None, ignores all active notifications.

    Returns:
        Number of rows updated.
    """
    with _get_connection() as conn:
        if notification_types:
            placeholders = ",".join("?" * len(notification_types))
            cursor = conn.execute(
                f"UPDATE notifications SET status = 'ignored' "
                f"WHERE status = 'active' AND type IN ({placeholders})",
                notification_types,
            )
        else:
            cursor = conn.execute(
                "UPDATE notifications SET status = 'ignored' WHERE status = 'active'"
            )
        updated_count = cursor.rowcount
    logger.info("Marked %d active notifications as ignored", updated_count)
    return updated_count


def mark_read(notification_id: int) -> None:
    """Mark a single notification as read (toast shown).

    Args:
        notification_id: Row id to mark as read.
    """
    with _get_connection() as conn:
        conn.execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ?",
            (notification_id,),
        )


def mark_all_read() -> None:
    """Mark all unread notifications as read (toast shown)."""
    with _get_connection() as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")


def delete_notification(notification_id: int) -> None:
    """Permanently delete a single notification from the database.

    Args:
        notification_id: Row id of the notification to delete.
    """
    with _get_connection() as conn:
        conn.execute("DELETE FROM notifications WHERE id = ?", (notification_id,))
    logger.info("Deleted notification %d", notification_id)


def delete_all_inactive_notifications() -> int:
    """Permanently delete all completed and ignored notifications.

    Active notifications are never deleted by this function.

    Returns:
        Number of rows deleted.
    """
    with _get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM notifications WHERE status IN ('completed', 'ignored')"
        )
        deleted_count = cursor.rowcount
    logger.info("Deleted %d inactive notifications", deleted_count)
    return deleted_count


def purge_old_non_active_notifications(days: int = 30) -> int:
    """Delete completed and ignored notifications older than *days* days.

    Active notifications are never purged regardless of age.

    Args:
        days: Notifications older than this many days are deleted.

    Returns:
        Number of rows deleted.
    """
    cutoff = (datetime.now(_IST) - timedelta(days=days)).isoformat()
    with _get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM notifications
            WHERE status != 'active' AND created_at < ?
            """,
            (cutoff,),
        )
        deleted_count = cursor.rowcount
    logger.info("Purged %d old non-active notifications (older than %d days)", deleted_count, days)
    return deleted_count


def check_and_update_cooldown(alert_key: str, cooldown_seconds: int = 3600) -> bool:
    """Return True and record the fire time if the cooldown has elapsed; False otherwise.

    The check and update are done in a single connection to avoid a TOCTOU race.
    Each alert_key row is updated in-place via ON CONFLICT — the table stays small
    (one row per distinct alert_key, max 3 rows for NIFTY_DELTA_ALERT_D0/D1/D2).

    Args:
        alert_key: Unique string identifying the alert (e.g. 'NIFTY_DELTA_ALERT_D1').
        cooldown_seconds: Minimum seconds between allowed fires (default 3600 = 1 hr).

    Returns:
        True if the alert is allowed to fire (cooldown elapsed or no prior record).
        False if the cooldown has not yet elapsed.
    """
    now_str = _now_ist()
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT last_fired FROM alert_cooldowns WHERE alert_key = ?",
            (alert_key,),
        ).fetchone()
        if row:
            elapsed = (
                datetime.fromisoformat(now_str) - datetime.fromisoformat(row["last_fired"])
            ).total_seconds()
            if elapsed < cooldown_seconds:
                return False
        conn.execute(
            """
            INSERT INTO alert_cooldowns (alert_key, last_fired)
            VALUES (?, ?)
            ON CONFLICT(alert_key) DO UPDATE SET last_fired = excluded.last_fired
            """,
            (alert_key, now_str),
        )
    return True


def _deserialise_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert sqlite3.Row objects to plain dicts with JSON fields decoded.

    Args:
        rows: List of sqlite3.Row objects from a notifications query.

    Returns:
        List of dicts with 'channels' and 'metadata' as Python objects.
    """
    result: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        entry["channels"] = json.loads(entry["channels"]) if entry["channels"] else []
        entry["metadata"] = json.loads(entry["metadata"]) if entry["metadata"] else {}
        result.append(entry)
    return result


def save_push_subscription(
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str = "",
) -> None:
    """Upsert a browser push subscription.

    Uses INSERT OR REPLACE so re-registrations from the same endpoint update
    the keys without creating duplicates.

    Args:
        endpoint: The push service URL from the browser's PushSubscription.
        p256dh: Client public key (base64url-encoded).
        auth: Auth secret (base64url-encoded).
        user_agent: Optional User-Agent string for diagnostics.
    """
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh     = excluded.p256dh,
                auth       = excluded.auth,
                user_agent = excluded.user_agent
            """,
            (endpoint, p256dh, auth, user_agent, _now_ist()),
        )


def delete_push_subscription(endpoint: str) -> None:
    """Remove a stale or explicitly unregistered push subscription.

    Args:
        endpoint: The push service URL to remove.
    """
    with _get_connection() as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        )


def get_all_push_subscriptions() -> list[dict[str, Any]]:
    """Return all stored push subscriptions.

    Returns:
        List of dicts with keys: endpoint, p256dh, auth, user_agent.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth, user_agent FROM push_subscriptions"
        ).fetchall()
    return [dict(row) for row in rows]
