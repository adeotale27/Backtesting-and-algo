"""GTT vs Position mismatch monitor.

Runs every 10 minutes (via APScheduler) and fires Telegram + web-push + in-app
notifications when pending GTT BUY orders would create an unintended long position.

Two alert conditions (mirrors the logic in the retired CLI script gtt_targets.py):
  - GTT_POSITION_EXCESS  : short position + GTT BUY qty > 0  (net would flip to LONG)
  - GTT_ORPHANED         : GTT BUY exists but position is FLAT (no open trade)

Deduplication: _known_mismatches tracks the set of active "{type}:{symbol}" keys.
Notifications fire only for newly appearing keys. If a mismatch resolves and reappears
the set shrinks then grows again, so the alert fires a second time.

Uses its own KiteConnect instance (built from the stored token in instruments.db),
same pattern as the scheduler jobs — independent of the Flask session.
"""

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_GTT_URL = "https://kite.zerodha.com/orders/gtt"

# Module-level state — reset naturally when process restarts.
_known_mismatches: set[str] = set()
_last_checked_at: datetime | None = None
_last_mismatch_list: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Pure logic helpers — testable without any live API calls
# ---------------------------------------------------------------------------


def build_nfo_nrml_positions_map(positions: list[dict[str, Any]]) -> dict[str, int]:
    """Filter net positions to NFO/NRML and return {tradingsymbol: quantity}.

    Args:
        positions: Raw list from KiteConnect.positions()["net"].

    Returns:
        Dict mapping tradingsymbol → net quantity (negative = short).
    """
    result: dict[str, int] = {}
    for position in positions:
        if position.get("exchange") != "NFO":
            continue
        if position.get("product") != "NRML":
            continue
        symbol: str = position["tradingsymbol"]
        quantity: int = int(position.get("quantity", 0))
        result[symbol] = quantity
    return result


def build_pending_nfo_gtt_map(gtts: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Filter pending NFO GTTs and aggregate BUY/SELL quantities per symbol.

    Skips already-executed GTTs (result != None) and non-NFO exchanges.

    Args:
        gtts: Raw list from KiteConnect.get_gtts().

    Returns:
        Dict mapping tradingsymbol → {"BUY": int, "SELL": int}.
    """
    result: dict[str, dict[str, int]] = {}
    for gtt in gtts:
        if not gtt.get("orders"):
            continue
        order = gtt["orders"][0]
        if order.get("exchange") != "NFO":
            continue
        if order.get("result") is not None:
            continue  # already executed
        symbol: str = order["tradingsymbol"]
        side: str = order.get("transaction_type", "BUY").upper()
        qty: int = int(order.get("quantity", 0))

        if symbol not in result:
            result[symbol] = {"BUY": 0, "SELL": 0}
        result[symbol][side] = result[symbol].get(side, 0) + qty
    return result


def find_mismatches(
    positions_map: dict[str, int],
    gtt_map: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Compute mismatch alerts between current positions and pending GTTs.

    Args:
        positions_map: Output of build_nfo_nrml_positions_map().
        gtt_map: Output of build_pending_nfo_gtt_map().

    Returns:
        List of mismatch dicts, each containing:
            type: "GTT_POSITION_EXCESS" or "GTT_ORPHANED"
            symbol: tradingsymbol string
            position_qty: current net qty (0 for ORPHANED)
            gtt_buy_qty: pending GTT BUY quantity
            net_if_triggered: position_qty + gtt_buy_qty
    """
    alerts: list[dict[str, Any]] = []

    for symbol, position_qty in positions_map.items():
        if symbol not in gtt_map:
            continue
        gtt_buy_qty: int = gtt_map[symbol]["BUY"]
        if gtt_buy_qty == 0:
            continue
        net_if_triggered: int = position_qty + gtt_buy_qty
        if net_if_triggered > 0:
            alerts.append(
                {
                    "type": "GTT_POSITION_EXCESS",
                    "symbol": symbol,
                    "position_qty": position_qty,
                    "gtt_buy_qty": gtt_buy_qty,
                    "net_if_triggered": net_if_triggered,
                }
            )

    for symbol, sides in gtt_map.items():
        gtt_buy_qty = sides["BUY"]
        if gtt_buy_qty == 0:
            continue
        if symbol not in positions_map:
            alerts.append(
                {
                    "type": "GTT_ORPHANED",
                    "symbol": symbol,
                    "position_qty": 0,
                    "gtt_buy_qty": gtt_buy_qty,
                    "net_if_triggered": gtt_buy_qty,
                }
            )

    return alerts


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


def _build_notification_payload(
    mismatch: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any]]:
    """Return (notification_type, title, body, metadata) for a mismatch dict.

    Args:
        mismatch: One item from find_mismatches().

    Returns:
        Tuple of (notification_type, title, body, metadata).
    """
    symbol: str = mismatch["symbol"]
    position_qty: int = mismatch["position_qty"]
    gtt_buy_qty: int = mismatch["gtt_buy_qty"]
    net_if_triggered: int = mismatch["net_if_triggered"]
    alert_type: str = mismatch["type"]

    metadata: dict[str, Any] = {
        "symbol": symbol,
        "position_qty": position_qty,
        "gtt_buy_qty": gtt_buy_qty,
        "net_if_triggered": net_if_triggered,
        "action_url": _GTT_URL,
        "action_label": "View GTTs",
    }

    if alert_type == "GTT_POSITION_EXCESS":
        title = f"GTT Over-Coverage: {symbol}"
        body = (
            f"⚠️ GTT Over-Coverage Alert\n\n"
            f"Symbol: {symbol}\n"
            f"Short qty: {position_qty}\n"
            f"GTT BUY qty: +{gtt_buy_qty}\n"
            f"Net if triggered: +{net_if_triggered} (LONG!)\n\n"
            f"\U0001f449 View GTTs: {_GTT_URL}"
        )
    else:  # GTT_ORPHANED
        title = f"Orphaned GTT: {symbol}"
        body = (
            f"⚠️ Orphaned GTT Alert\n\n"
            f"Symbol: {symbol}\n"
            f"GTT BUY qty: +{gtt_buy_qty}\n"
            f"Position: FLAT (no open trade)\n\n"
            f"Trigger will create a fresh naked long!\n\n"
            f"\U0001f449 View GTTs: {_GTT_URL}"
        )

    return alert_type, title, body, metadata


# ---------------------------------------------------------------------------
# Orchestrator — called by APScheduler
# ---------------------------------------------------------------------------


def _build_kite_client():
    """Return an authenticated KiteConnect instance.

    In a Flask request context (e.g. /gtt_monitor/debug, /gtt_monitor/check):
    reads ``session["access_token"]`` — same token the rest of the app uses,
    so it is always valid when the user is logged in.

    In a background/scheduler context (no request context):
    falls back to the token stored in instruments.db by _set_kite_session().

    Returns:
        KiteConnect instance with a valid access token set.

    Raises:
        RuntimeError: If api_key is not found or no token is available.
    """
    import configparser
    from kiteconnect import KiteConnect

    cfg = configparser.ConfigParser()
    # configfile.ini lives in the same directory as this file (vibhu/)
    cfg.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "configfile.ini"))
    api_key: str = cfg.get("kite_login_details", "api_key", fallback="").strip()
    if not api_key:
        raise RuntimeError("api_key not found in configfile.ini [kite_login_details]")

    access_token: str | None = None

    # Try Flask session first (only available during a request)
    try:
        from flask import has_request_context, session as flask_session
        if has_request_context():
            access_token = flask_session.get("access_token")
    except ImportError:
        pass

    # Fall back to the token persisted in instruments.db (used by scheduler jobs)
    if not access_token:
        import instrument_cache
        access_token = instrument_cache.get_kite_token()

    if not access_token:
        raise RuntimeError("No Kite access token available — log in to Kite first")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def run_gtt_monitor_check(*, force: bool = False) -> list[dict[str, Any]]:
    """Fetch live data, find mismatches, notify for new ones, update state.

    Called by APScheduler every 10 minutes. Guards with is_market_open() so
    it silently skips outside trading hours, unless force=True.

    Builds its own KiteConnect instance from the stored token (instruments.db)
    so it works independently of the Flask session.

    Args:
        force: If True, bypass the is_market_open() guard. Use for manual
            trigger / debugging via the /gtt_monitor/check endpoint.

    Returns:
        Full current mismatch list (empty list outside market hours when not forced).

    Never raises — all exceptions are caught and logged.
    """
    global _known_mismatches, _last_checked_at, _last_mismatch_list

    try:
        from common_lib import is_market_open
    except ImportError as exc:
        logger.error("gtt_monitor: failed to import common_lib — %s", exc)
        return []

    if not force and not is_market_open():
        logger.debug("gtt_monitor: market closed, skipping check")
        return []

    _last_checked_at = datetime.now(tz=_IST)

    try:
        kite = _build_kite_client()
    except Exception as exc:
        logger.error("gtt_monitor: could not build Kite client — %s", exc)
        return _last_mismatch_list

    try:
        positions_raw = kite.positions()
        net_positions: list[dict[str, Any]] = positions_raw.get("net", [])
    except Exception as exc:
        logger.error("gtt_monitor: failed to fetch positions — %s", exc, exc_info=True)
        return _last_mismatch_list  # return stale rather than clearing

    try:
        gtts_raw: list[dict[str, Any]] = kite.get_gtts()
    except Exception as exc:
        logger.error("gtt_monitor: failed to fetch GTTs — %s", exc, exc_info=True)
        return _last_mismatch_list

    positions_map = build_nfo_nrml_positions_map(net_positions)
    gtt_map = build_pending_nfo_gtt_map(gtts_raw)
    current_mismatches: list[dict[str, Any]] = find_mismatches(positions_map, gtt_map)

    current_keys: set[str] = {f"{m['type']}:{m['symbol']}" for m in current_mismatches}
    new_keys: set[str] = current_keys - _known_mismatches

    for mismatch in current_mismatches:
        key = f"{mismatch['type']}:{mismatch['symbol']}"
        if key not in new_keys:
            continue
        try:
            from notifications.service import dispatch as _notify

            notif_type, title, body, metadata = _build_notification_payload(mismatch)
            _notify(notif_type, title=title, body=body, metadata=metadata)
            logger.warning("gtt_monitor: dispatched %s for %s", notif_type, mismatch["symbol"])
        except Exception as exc:
            logger.error(
                "gtt_monitor: notification dispatch failed for %s — %s",
                mismatch["symbol"],
                exc,
                exc_info=True,
            )

    _known_mismatches = current_keys
    _last_mismatch_list = current_mismatches

    if current_mismatches:
        logger.warning("gtt_monitor: %d active mismatch(es) found", len(current_mismatches))
    else:
        logger.info("gtt_monitor: no mismatches found")

    return current_mismatches


def get_current_mismatch_snapshot() -> dict[str, Any]:
    """Return cached mismatch state for the /gtt_monitor/status Flask endpoint.

    Does NOT make any API calls — returns the last computed state.

    Returns:
        Dict with keys:
            mismatches: list of current mismatch dicts
            last_checked: ISO timestamp string or None
            count: int
    """
    return {
        "mismatches": _last_mismatch_list,
        "last_checked": _last_checked_at.isoformat() if _last_checked_at else None,
        "count": len(_last_mismatch_list),
    }
