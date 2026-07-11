"""Position Guard dashboard: surfaces symbols carrying unreviewed long exposure.

Combines the currently held position, OPEN regular BUY orders, and active GTT
BUY orders per symbol so duplicate/unintended buy-side commitments (e.g. a
manual order stacked on top of an algo order) can be spotted and pruned before
they fill into an oversized position.
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from position_guard import db as position_guard_db
from position_guard.detector import scan_long_exposure

logger = logging.getLogger(__name__)

position_guard_bp = Blueprint(
    "position_guard",
    __name__,
    url_prefix="/position-guard",
    template_folder="templates",
)


def _get_kite_client():
    """Return the main account's session-authenticated Kite client.

    Returns:
        MonitoredKite: The same client used elsewhere in flask_app for
        request-scoped Kite calls.
    """
    from flask_app import get_kite_client

    return get_kite_client()


@position_guard_bp.route("/")
def dashboard() -> str:
    """Render the Position Guard dashboard page."""
    return render_template("position_guard/dashboard.html")


@position_guard_bp.route("/api/data")
def api_data():
    """Return current long-exposure rows, split into active and ignored.

    Calls Kite live — no cache — so the dashboard always reflects the freshest
    state. Called by the dashboard on load and every 60 s.

    Returns:
        JSON with keys: rows (list, not currently ignored), ignored (list, from
        position_guard_db.list_ignored), checked_at (ISO str), error (str, only
        on failure).
    """
    from common_lib import get_ist_now

    kite = _get_kite_client()
    try:
        all_rows = scan_long_exposure(kite)
    except Exception as exc:
        logger.error("api_data: scan failed — %s", exc, exc_info=True)
        return jsonify(rows=[], ignored=[], checked_at=get_ist_now().isoformat(), error=str(exc))

    active_rows = [
        row for row in all_rows
        if not position_guard_db.is_ignored(row["tradingsymbol"], row["exchange"], row["fingerprint"])
    ]

    return jsonify(
        rows=active_rows,
        ignored=position_guard_db.list_ignored(),
        checked_at=get_ist_now().isoformat(),
    )


@position_guard_bp.route("/api/ignore", methods=["POST"])
def api_ignore():
    """Ignore a symbol for its current fingerprint.

    Request body (JSON): tradingsymbol (str), exchange (str), fingerprint (str).

    Returns:
        JSON {"ok": true} or {"error": "..."}.
    """
    body = request.get_json(silent=True) or {}
    tradingsymbol = str(body.get("tradingsymbol", "")).strip()
    exchange = str(body.get("exchange", "")).strip()
    fingerprint = str(body.get("fingerprint", "")).strip()

    if not tradingsymbol or not fingerprint:
        return jsonify({"error": "tradingsymbol and fingerprint are required"}), 400

    position_guard_db.ignore_symbol(tradingsymbol, exchange, fingerprint)
    return jsonify({"ok": True})


@position_guard_bp.route("/api/unignore", methods=["POST"])
def api_unignore():
    """Remove a symbol's ignore, moving it back into the main table.

    Request body (JSON): tradingsymbol (str), exchange (str).

    Returns:
        JSON {"ok": true} or {"error": "..."}.
    """
    body = request.get_json(silent=True) or {}
    tradingsymbol = str(body.get("tradingsymbol", "")).strip()
    exchange = str(body.get("exchange", "")).strip()

    if not tradingsymbol:
        return jsonify({"error": "tradingsymbol is required"}), 400

    position_guard_db.unignore_symbol(tradingsymbol, exchange)
    return jsonify({"ok": True})


@position_guard_bp.route("/api/orders/<order_id>/modify", methods=["POST"])
def api_modify_order(order_id: str):
    """Modify a regular OPEN order.

    Request body (JSON): variety (str), quantity (int, optional),
        order_type (str, optional), price (float, optional),
        trigger_price (float, optional).

    Returns:
        JSON {"ok": true} or {"error": "..."}.
    """
    body = request.get_json(silent=True) or {}
    variety = body.get("variety") or "regular"
    new_quantity = body.get("quantity")
    new_order_type = body.get("order_type")
    new_price = float(body.get("price") or 0)
    new_trigger_price = float(body.get("trigger_price") or 0)

    modify_kwargs: dict = {"variety": variety, "order_id": order_id}
    if new_quantity is not None:
        modify_kwargs["quantity"] = int(new_quantity)
    if new_order_type is not None:
        modify_kwargs["order_type"] = new_order_type
    if new_price > 0:
        modify_kwargs["price"] = new_price
    if new_trigger_price > 0:
        modify_kwargs["trigger_price"] = new_trigger_price

    kite = _get_kite_client()
    try:
        kite.modify_order(**modify_kwargs)
        logger.info("api_modify_order: order %s modified — %s", order_id, modify_kwargs)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("api_modify_order: failed for order %s — %s", order_id, exc)
        return jsonify({"error": str(exc)}), 500


@position_guard_bp.route("/api/orders/<order_id>/cancel", methods=["POST"])
def api_cancel_order(order_id: str):
    """Cancel a regular OPEN order.

    Request body (JSON): variety (str, default "regular").

    Returns:
        JSON {"ok": true} or {"error": "..."}.
    """
    body = request.get_json(silent=True) or {}
    variety = body.get("variety") or "regular"

    kite = _get_kite_client()
    try:
        kite.cancel_order(variety=variety, order_id=order_id)
        logger.info("api_cancel_order: order %s cancelled", order_id)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("api_cancel_order: failed for order %s — %s", order_id, exc)
        return jsonify({"error": str(exc)}), 500


@position_guard_bp.route("/api/gtts/<int:trigger_id>/modify", methods=["POST"])
def api_modify_gtt(trigger_id: int):
    """Modify an existing GTT's trigger price, limit price, and/or quantity.

    Request body (JSON): tradingsymbol (str), exchange (str),
        transaction_type ("BUY"/"SELL"), product (str), trigger_price (float),
        limit_price (float), quantity (int), last_price (float, required by Zerodha).

    Returns:
        JSON {"ok": true} or {"error": "..."}.
    """
    body = request.get_json(silent=True) or {}

    tradingsymbol = (body.get("tradingsymbol") or "").strip().upper()
    exchange = (body.get("exchange") or "").strip().upper()
    transaction_type = (body.get("transaction_type") or "").strip().upper()
    product = (body.get("product") or "NRML").strip().upper()

    try:
        quantity = int(body.get("quantity") or 0)
        trigger_price = float(body.get("trigger_price") or 0)
        limit_price = float(body.get("limit_price") or 0)
        last_price = float(body.get("last_price") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "quantity, trigger_price, limit_price, and last_price must be numeric"}), 400

    if not tradingsymbol or not exchange:
        return jsonify({"error": "tradingsymbol and exchange are required"}), 400
    if transaction_type not in ("BUY", "SELL"):
        return jsonify({"error": "transaction_type must be BUY or SELL"}), 400
    if quantity <= 0 or trigger_price <= 0 or limit_price <= 0 or last_price <= 0:
        return jsonify({"error": "quantity, trigger_price, limit_price, and last_price must be positive"}), 400

    kite = _get_kite_client()
    try:
        kite.modify_gtt(
            trigger_id=trigger_id,
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_values=[trigger_price],
            last_price=last_price,
            orders=[{
                "transaction_type": transaction_type,
                "quantity": quantity,
                "order_type": kite.ORDER_TYPE_LIMIT,
                "product": product,
                "price": limit_price,
            }],
        )
        logger.info(
            "api_modify_gtt: trigger_id=%d %s trigger=%.2f limit=%.2f qty=%d",
            trigger_id, tradingsymbol, trigger_price, limit_price, quantity,
        )
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("api_modify_gtt: trigger_id=%d failed — %s", trigger_id, exc)
        return jsonify({"error": str(exc)}), 500


@position_guard_bp.route("/api/gtts/<int:trigger_id>/delete", methods=["POST"])
def api_delete_gtt(trigger_id: int):
    """Delete (cancel) a GTT by trigger ID.

    Returns:
        JSON {"ok": true} or {"error": "..."}.
    """
    kite = _get_kite_client()
    try:
        kite.delete_gtt(trigger_id)
        logger.info("api_delete_gtt: deleted trigger_id=%d", trigger_id)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("api_delete_gtt: trigger_id=%d failed — %s", trigger_id, exc)
        return jsonify({"error": str(exc)}), 500
