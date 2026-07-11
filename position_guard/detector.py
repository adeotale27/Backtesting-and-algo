"""Long-exposure duplicate detection and alert engine — OPTIONS ONLY.

Scans the currently held OPTION positions, all OPEN regular option BUY orders,
and all active GTT option BUY orders, and groups them per tradingsymbol.
Equities and futures are explicitly out of scope (see ``_is_option_symbol``).
A symbol is surfaced only if netting its existing (signed) position against all
pending BUY-side commitments would leave a projected net quantity greater than
zero, so the user can review whether multiple independent buy-side commitments
(manual + algo, or two algos) are about to stack into an unintended oversized
long position.

Primary entry points:
  - ``scan_long_exposure(kite_client)``       — used by the Flask blueprint (sync, returns list)
  - ``check_and_notify_position_guard()``     — called by APScheduler every 5 min
"""

import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_F_AND_O_EXCHANGES: frozenset[str] = frozenset({"NFO", "BFO"})
_NOTIFICATION_COOLDOWN_SECONDS: int = 1800  # 30-minute cooldown per symbol


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def _fingerprint(order_ids: list[str], gtt_trigger_ids: list[str], position_qty_signed: int) -> str:
    """Build a stable fingerprint over the sources contributing to a symbol's exposure.

    The fingerprint changes whenever the *set* of contributing orders/GTTs changes
    (an order fills, is cancelled, or a new one is placed) or the existing position's
    signed quantity changes at all (including a short position being partially
    closed, which changes how much of the pending buys it would absorb). This is
    used to auto-expire a previously-ignored symbol.

    Args:
        order_ids: Regular order IDs contributing BUY-side exposure.
        gtt_trigger_ids: GTT trigger IDs contributing BUY-side exposure.
        position_qty_signed: The existing position's signed quantity (negative for
            short, positive for long, 0 if flat).

    Returns:
        A SHA1 hex digest string uniquely identifying this combination of sources.
    """
    parts = sorted(f"ORDER:{oid}" for oid in order_ids)
    parts += sorted(f"GTT:{tid}" for tid in gtt_trigger_ids)
    if position_qty_signed != 0:
        parts.append(f"POSITION:{position_qty_signed}")
    digest_input = "|".join(parts)
    return hashlib.sha1(digest_input.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# GTT enrichment
# ---------------------------------------------------------------------------


def _enrich_gtt(gtt: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Extract BUY-side exposure info from a single GTT, with best-effort algo attribution.

    GTTs do not carry a Kite order tag, so algo attribution is looked up via the
    trigger_id → algo_source mapping persisted by the copytrade "New Order" modal
    (``instrument_cache.save_gtt_algo_tag``). Main-account GTTs placed by
    ``common_lib.place_gtt_order()`` are not persisted there (only tracked in an
    in-memory dict that is lost on restart), so those fall back to an
    "unknown source" label — this is a known limitation, not a bug.

    Args:
        gtt: A single GTT dict as returned by ``kite.get_gtts()``.

    Returns:
        Enriched dict with keys: trigger_id, tradingsymbol, exchange, buy_quantity,
        trigger_values, child_orders, algo_name, algo_category, is_algo. Returns
        None if the GTT has no BUY-side child order (nothing to surface here).
    """
    import instrument_cache

    condition = gtt.get("condition", {}) or {}
    child_orders = gtt.get("orders", []) or []
    buy_children = [o for o in child_orders if str(o.get("transaction_type")) == "BUY"]
    if not buy_children:
        return None

    trigger_id = str(gtt.get("id", gtt.get("trigger_id", "")))
    algo_source = instrument_cache.get_gtt_algo_source(f"UI_GTT_{trigger_id}")
    if algo_source:
        from trade_journal import classify_source

        source_info = classify_source(algo_source)
        algo_name = source_info["algo_name"]
        algo_category = source_info["category"]
        is_algo = source_info["is_algo"]
    else:
        algo_name = "GTT (source unknown)"
        algo_category = "Other"
        is_algo = False

    return {
        "trigger_id": trigger_id,
        "status": str(gtt.get("status", "")),
        "tradingsymbol": str(condition.get("tradingsymbol", "")),
        "exchange": str(condition.get("exchange", "")),
        "trigger_values": condition.get("trigger_values", []),
        "buy_quantity": sum(int(o.get("quantity", 0) or 0) for o in buy_children),
        "child_orders": child_orders,
        "algo_name": algo_name,
        "algo_category": algo_category,
        "is_algo": is_algo,
    }


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------


def scan_long_exposure(kite_client: Any) -> list[dict[str, Any]]:
    """Build one row per OPTION symbol whose projected net position would be long.

    Options only (CE/PE) — equities and futures are excluded via
    ``_is_option_symbol``, since a stock and an option on the same underlying are
    different instruments and must never be netted against each other. For each
    qualifying symbol, the existing position's *signed* quantity (positive for
    long, negative for short) is netted against all pending BUY-side
    commitments — every OPEN regular BUY order and every active GTT with a BUY
    child order (both restricted to F&O exchanges, NFO/BFO). A symbol is only
    surfaced if that projected net quantity would be strictly greater than
    zero — e.g. an existing short position that pending buys would merely
    flatten to zero is not flagged, since there is no risk of an unintended
    long position.

    Args:
        kite_client: Authenticated KiteConnect or MonitoredKite instance.

    Returns:
        List of row dicts, each containing:
          {
            "tradingsymbol": str,
            "exchange": str,
            "instrument_token": str,
            "instrument_display": str,
            "position_qty": int,            # existing LONG position only (0 if flat or short)
            "regular_orders": list[dict],   # enriched OPEN BUY orders
            "gtts": list[dict],             # enriched GTTs with BUY child orders
            "total_long_qty": int,          # projected net quantity if everything fills
            "source_count": int,
            "fingerprint": str,
          }
        Returns [] on API error (after logging).
    """
    from duplicate_order_monitor import parse_instrument_display
    from trade_journal import classify_source

    try:
        positions_raw = kite_client.positions()
    except Exception as exc:
        logger.error("scan_long_exposure: kite.positions() failed — %s", exc)
        return []

    try:
        all_orders: list[dict] = kite_client.orders()
    except Exception as exc:
        logger.error("scan_long_exposure: kite.orders() failed — %s", exc)
        return []

    try:
        all_gtts: list[dict] = kite_client.get_gtts()
    except Exception as exc:
        logger.error("scan_long_exposure: kite.get_gtts() failed — %s", exc)
        return []

    # Any non-flat OPTION position (long or short) is tracked so its signed quantity
    # can be netted against pending buys — a short position that pending buys would
    # only flatten (not flip positive) must not be flagged. Equities and futures are
    # out of scope for this guard.
    all_positions = {
        str(p["tradingsymbol"]): p
        for p in positions_raw.get("net", [])
        if int(p.get("quantity", 0) or 0) != 0
        and p.get("exchange") in _F_AND_O_EXCHANGES
        and _is_option_symbol(str(p["tradingsymbol"]))
    }

    open_buy_orders = [
        order for order in all_orders
        if order.get("status") == "OPEN"
        and order.get("exchange") in _F_AND_O_EXCHANGES
        and str(order.get("transaction_type")) == "BUY"
        and _is_option_symbol(str(order.get("tradingsymbol", "")))
    ]

    orders_by_symbol: dict[str, list[dict]] = {}
    for order in open_buy_orders:
        orders_by_symbol.setdefault(str(order["tradingsymbol"]), []).append(order)

    gtts_by_symbol: dict[str, list[dict]] = {}
    for gtt in all_gtts:
        if str(gtt.get("status", "")).lower() != "active":
            continue
        enriched = _enrich_gtt(gtt)
        if (
            enriched is None
            or enriched["exchange"] not in _F_AND_O_EXCHANGES
            or not _is_option_symbol(enriched["tradingsymbol"])
        ):
            continue
        gtts_by_symbol.setdefault(enriched["tradingsymbol"], []).append(enriched)

    symbols = set(all_positions) | set(orders_by_symbol) | set(gtts_by_symbol)

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        position = all_positions.get(symbol)
        position_qty_signed = int(position.get("quantity", 0) or 0) if position else 0

        enriched_orders: list[dict[str, Any]] = []
        for order in orders_by_symbol.get(symbol, []):
            tag = (order.get("tag") or "Unknown").strip() or "Unknown"
            source_info = classify_source(tag)
            enriched_orders.append({
                "order_id": str(order.get("order_id", "")),
                "variety": str(order.get("variety", "regular") or "regular"),
                "price": float(order.get("price", 0) or 0),
                "quantity": int(order.get("quantity", 0) or 0),
                "filled_quantity": int(order.get("filled_quantity", 0) or 0),
                "product": str(order.get("product", "") or ""),
                "tag": tag,
                "algo_name": source_info["algo_name"],
                "algo_category": source_info["category"],
                "is_algo": source_info["is_algo"],
                "placed_at": str(order.get("order_timestamp", "") or ""),
            })

        gtt_rows = gtts_by_symbol.get(symbol, [])

        if position:
            exchange = str(position.get("exchange", "") or "")
            instrument_token = str(position.get("instrument_token", "") or "")
        elif symbol in orders_by_symbol:
            exchange = str(orders_by_symbol[symbol][0].get("exchange", "") or "")
            instrument_token = str(orders_by_symbol[symbol][0].get("instrument_token", "") or "")
        elif gtt_rows:
            exchange = gtt_rows[0]["exchange"]
            instrument_token = _lookup_instrument_token(symbol)
        else:
            exchange = ""
            instrument_token = ""

        buy_qty_total = sum(o["quantity"] for o in enriched_orders) + sum(
            g["buy_quantity"] for g in gtt_rows
        )
        projected_net_qty = position_qty_signed + buy_qty_total
        if projected_net_qty <= 0:
            # Pending buys would only flatten (or leave short) an existing short
            # position — no risk of an unintended long position, so skip.
            continue

        position_qty_long = position_qty_signed if position_qty_signed > 0 else 0
        source_count = (1 if position_qty_long > 0 else 0) + len(enriched_orders) + len(gtt_rows)

        fingerprint = _fingerprint(
            order_ids=[o["order_id"] for o in enriched_orders],
            gtt_trigger_ids=[g["trigger_id"] for g in gtt_rows],
            position_qty_signed=position_qty_signed,
        )

        instrument_info = parse_instrument_display(symbol)

        rows.append({
            "tradingsymbol": symbol,
            "exchange": str(exchange or ""),
            "instrument_token": instrument_token,
            "instrument_display": instrument_info["display"],
            "underlying": instrument_info["underlying"],
            "position_qty": position_qty_long,
            "regular_orders": enriched_orders,
            "gtts": gtt_rows,
            "total_long_qty": projected_net_qty,
            "source_count": source_count,
            "fingerprint": fingerprint,
        })

    rows.sort(key=lambda row: (_underlying_sort_rank(row["underlying"]), row["tradingsymbol"]))

    logger.info(
        "scan_long_exposure: %d symbol(s) with projected long exposure found "
        "(%d non-flat positions, %d order groups, %d GTT groups)",
        len(rows), len(all_positions), len(orders_by_symbol), len(gtts_by_symbol),
    )
    return rows


def _is_option_symbol(tradingsymbol: str) -> bool:
    """Return True only for CE/PE option symbols — this guard is options-only.

    Every Kite option trading symbol (index or single-stock) ends in "CE" or "PE"
    by construction, so a plain suffix check is used rather than
    ``duplicate_order_monitor.parse_instrument_display``, which only recognises a
    handful of known index underlyings and would otherwise misclassify individual
    stock options (e.g. "RELIANCE24DEC2900CE") as non-options. Equities and
    futures are out of scope for this guard.

    Args:
        tradingsymbol: The Kite trading symbol to check.

    Returns:
        True if the symbol ends in "CE" or "PE".
    """
    return tradingsymbol.strip().upper().endswith(("CE", "PE"))


_UNDERLYING_SORT_ORDER: list[str] = ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "BANKEX"]


def _underlying_sort_rank(underlying: str) -> int:
    """Rank a symbol's underlying for display ordering.

    NIFTY and SENSEX (and their weekly/monthly options and futures) sort first,
    other known index underlyings next, and individual stocks (no recognised
    underlying) sort last.

    Args:
        underlying: The parsed underlying name, or "" for stocks/unrecognised symbols.

    Returns:
        A sort rank — lower sorts first.
    """
    try:
        return _UNDERLYING_SORT_ORDER.index(underlying)
    except ValueError:
        return len(_UNDERLYING_SORT_ORDER)


def _lookup_instrument_token(tradingsymbol: str) -> str:
    """Resolve an instrument_token for a symbol only present via a GTT (no order/position row).

    Args:
        tradingsymbol: The Kite trading symbol to resolve.

    Returns:
        The instrument_token as a string, or "" if not found in the instrument cache.
    """
    import instrument_cache

    try:
        instruments_by_symbol = instrument_cache.get_all_fut_opt_instruments_by_symbol()
        info = instruments_by_symbol.get(tradingsymbol)
        return str(info["instrument_token"]) if info else ""
    except Exception as exc:
        logger.warning("_lookup_instrument_token: lookup failed for %s — %s", tradingsymbol, exc)
        return ""


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------


def check_and_notify_position_guard() -> None:
    """Scheduler entry point: scan for long-exposure symbols and fire notifications.

    - Runs only during market hours (9:20-15:25 IST, weekdays), same window as
      ``duplicate_order_monitor.check_and_notify_duplicates``.
    - Skips any symbol whose current fingerprint matches an active ignore row in
      ``position_guard.db`` (i.e. the user has already reviewed this exact set of
      contributing orders/GTTs).
    - Uses a 30-minute per-symbol cooldown to prevent notification spam across
      scan cycles.

    Never raises — all exceptions are caught and logged.
    """
    from common_lib import get_ist_now
    from duplicate_order_monitor import _build_kite_client_from_db
    from notifications import database as notif_db
    from notifications.service import dispatch
    from position_guard import db as position_guard_db

    now_ist = get_ist_now()

    if now_ist.weekday() >= 5:
        logger.debug("check_and_notify_position_guard: weekend — skipping")
        return

    market_open = now_ist.replace(hour=9, minute=20, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=25, second=0, microsecond=0)
    if not (market_open <= now_ist <= market_close):
        logger.debug("check_and_notify_position_guard: outside market hours — skipping")
        return

    logger.info("check_and_notify_position_guard: running scan")

    kite_client = _build_kite_client_from_db()
    if kite_client is None:
        return

    try:
        rows = scan_long_exposure(kite_client)
    except Exception as exc:
        logger.error("check_and_notify_position_guard: scan failed — %s", exc, exc_info=True)
        return

    for row in rows:
        symbol = row["tradingsymbol"]
        exchange = row["exchange"]
        fingerprint = row["fingerprint"]

        if position_guard_db.is_ignored(symbol, exchange, fingerprint):
            logger.debug("check_and_notify_position_guard: %s ignored — skipping", symbol)
            continue

        cooldown_key = f"POSITION_GUARD_{symbol}"
        if not notif_db.check_and_update_cooldown(cooldown_key, _NOTIFICATION_COOLDOWN_SECONDS):
            logger.debug(
                "check_and_notify_position_guard: cooldown active for %s — suppressed", symbol
            )
            continue

        algo_names = list(dict.fromkeys(
            [o["algo_name"] for o in row["regular_orders"]] + [g["algo_name"] for g in row["gtts"]]
        ))
        algos_str = " + ".join(algo_names) if algo_names else "Existing position"

        title = f"Long Exposure: {symbol}"
        body = (
            f"{row['source_count']} source(s), total qty {row['total_long_qty']} — {algos_str}\n"
            f"{row['instrument_display']}"
        )

        try:
            dispatch(
                notification_type="POSITION_GUARD_ALERT",
                title=title,
                body=body,
                metadata={
                    "symbol": symbol,
                    "instrument_display": row["instrument_display"],
                    "source_count": row["source_count"],
                    "total_long_qty": row["total_long_qty"],
                    "algos": algo_names,
                    "action_url": "/position-guard",
                    "action_label": "View Dashboard",
                },
            )
            logger.info(
                "check_and_notify_position_guard: dispatched POSITION_GUARD_ALERT for %s "
                "(%d sources, qty %d, algos: %s)",
                symbol, row["source_count"], row["total_long_qty"], algos_str,
            )
        except Exception as exc:
            logger.error(
                "check_and_notify_position_guard: dispatch failed for %s — %s", symbol, exc
            )
