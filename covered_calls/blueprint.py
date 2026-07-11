"""Flask blueprint for the Covered Calls management dashboard.

URL prefix: /covered_calls

Endpoints
---------
GET  /                    — Dashboard HTML
GET  /api/status          — Full dashboard data (holdings + active CCs + candidates)
GET  /api/candidates      — Live candidate strikes for one symbol
POST /api/place           — Place a sell-CE covered call order
GET  /api/config          — Read current config
POST /api/config          — Save config (otm_pct, min_premium_per_lot)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from typing import Any

from flask import Blueprint, jsonify, render_template, request, session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from covered_calls import covered_calls_lib as cc_lib
from covered_calls import pending_orders_db

logger = logging.getLogger(__name__)

# Initialise the pending-orders DB table on first import
pending_orders_db.init_db()

covered_calls_bp = Blueprint(
    "covered_calls",
    __name__,
    template_folder="templates",
    url_prefix="/covered_calls",
)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "covered_calls_config.json",
)

_DEFAULT_CONFIG: dict[str, Any] = {
    "otm_pct": 5.0,
    "min_premium_per_lot": 3000.0,
    "per_symbol": {},
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, Any]:
    """Load config from disk, falling back to defaults on any error."""
    if not os.path.exists(_CONFIG_PATH):
        return dict(_DEFAULT_CONFIG)
    try:
        with open(_CONFIG_PATH, "r") as fh:
            loaded = json.load(fh)
        return {**_DEFAULT_CONFIG, **loaded}
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load covered_calls_config.json — using defaults")
        return dict(_DEFAULT_CONFIG)


def _save_config(config: dict[str, Any]) -> None:
    """Persist config to disk atomically via temp-file rename.

    Args:
        config: Config dict to persist.
    """
    tmp_path = _CONFIG_PATH + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(config, fh, indent=2)
    os.replace(tmp_path, _CONFIG_PATH)


# ---------------------------------------------------------------------------
# Kite client resolution
# ---------------------------------------------------------------------------


def _resolve_kite_client() -> Any | None:
    """Return a usable KiteConnect client from session or stored token.

    Returns:
        Authenticated KiteConnect instance, or None if unavailable.
    """
    try:
        import instrument_cache
        from kiteconnect import KiteConnect
        from common_lib import api_key as kite_api_key
        from kite_api_monitor import MonitoredKite

        token = session.get("access_token") or instrument_cache.get_kite_token()
        if not token:
            return None
        client = MonitoredKite(KiteConnect(api_key=kite_api_key), account_id="main")
        client.set_access_token(token)
        return client
    except Exception:
        logger.exception("_resolve_kite_client: could not build kite client")
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@covered_calls_bp.route("/")
def dashboard() -> str:
    """Render the covered calls dashboard page."""
    return render_template("covered_calls/dashboard.html")


def _parse_expiry_param(raw: str | None) -> date | None:
    """Parse an ISO-date expiry query parameter, returning None on any error.

    Args:
        raw: Raw string value from the query string, e.g. "2026-06-24".

    Returns:
        Parsed date, or None if the string is missing or malformed.
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        logger.warning("Invalid expiry param %r — ignoring", raw)
        return None


@covered_calls_bp.route("/api/expiries")
def expiries() -> Any:
    """Return all available monthly expiries for the dropdown selector.

    Returns:
        JSON list of expiry objects with expiry, label, working_days, recommended fields.
    """
    try:
        available = cc_lib.get_available_expiries(months_ahead=6)
    except Exception:
        logger.exception("/api/expiries failed")
        return jsonify({"error": "Failed to compute available expiries"}), 500

    return jsonify(
        [
            {
                "expiry": exp.isoformat(),
                "label": label,
                "working_days": working_days,
                "recommended": is_recommended,
            }
            for exp, label, working_days, is_recommended in available
        ]
    )


@covered_calls_bp.route("/api/status")
def status() -> Any:
    """Return full dashboard data: holdings, active CCs, and candidates.

    Query params:
        otm_pct (float, optional): Override OTM % for this request.
        min_premium (float, optional): Override min premium for this request.

    Returns:
        JSON with rows, expiry metadata, and summary counts.
    """
    kite_client = _resolve_kite_client()
    if kite_client is None:
        return jsonify({"error": "Not authenticated — connect Kite first"}), 401

    config = _load_config()

    try:
        otm_pct = float(request.args.get("otm_pct", config["otm_pct"]))
        min_premium = float(request.args.get("min_premium", config["min_premium_per_lot"]))
    except (ValueError, TypeError):
        otm_pct = float(config["otm_pct"])
        min_premium = float(config["min_premium_per_lot"])

    expiry_override = _parse_expiry_param(request.args.get("expiry"))

    try:
        rows, expiry, label, working_days = cc_lib.build_dashboard_data(
            kite_client=kite_client,
            otm_pct=otm_pct,
            min_premium_per_lot=min_premium,
            expiry_override=expiry_override,
        )
    except Exception:
        logger.exception("/api/covered_calls/status failed")
        return jsonify({"error": "Failed to fetch covered calls data"}), 500

    total_premium_collected = sum(
        (row.active_cc.avg_sell_price * row.active_cc.short_qty)
        for row in rows
        if row.active_cc is not None
    )

    return jsonify(
        {
            "rows": [cc_lib.row_to_dict(r) for r in rows],
            "expiry": expiry.isoformat(),
            "expiry_label": label,
            "working_days_remaining": working_days,
            "summary": {
                "total_eligible": len(rows),
                "active": sum(1 for r in rows if r.status == "ACTIVE"),
                "partial": sum(1 for r in rows if r.status == "PARTIAL"),
                "missing": sum(1 for r in rows if r.status == "MISSING"),
                "total_premium_collected": round(total_premium_collected, 2),
            },
        }
    )


@covered_calls_bp.route("/api/candidates")
def candidates() -> Any:
    """Return live candidate OTM strikes for a single symbol.

    Query params:
        symbol (str): Underlying stock symbol, e.g. "TCS".
        otm_pct (float, optional): OTM percentage override.
        min_premium (float, optional): Min premium override.

    Returns:
        JSON list of candidate option objects.
    """
    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol query parameter required"}), 400

    kite_client = _resolve_kite_client()
    if kite_client is None:
        return jsonify({"error": "Not authenticated"}), 401

    config = _load_config()
    try:
        otm_pct = float(request.args.get("otm_pct", config["otm_pct"]))
        min_premium = float(request.args.get("min_premium", config["min_premium_per_lot"]))
    except (ValueError, TypeError):
        otm_pct = float(config["otm_pct"])
        min_premium = float(config["min_premium_per_lot"])

    # Apply per-symbol override
    per_symbol = config.get("per_symbol", {})
    if symbol in per_symbol:
        otm_pct = float(per_symbol[symbol].get("otm_pct", otm_pct))

    # Fetch LTP for underlying
    try:
        ltp_result = kite_client.ltp([f"NSE:{symbol}"])
        ltp = float(ltp_result.get(f"NSE:{symbol}", {}).get("last_price", 0) or 0)
    except Exception:
        logger.exception("LTP fetch failed for %s", symbol)
        return jsonify({"error": f"Could not fetch LTP for {symbol}"}), 500

    if ltp <= 0:
        return jsonify({"error": f"Zero LTP for {symbol} — market may be closed"}), 400

    lot_size = cc_lib._get_lot_size_for_symbol(symbol) or 1
    expiry_override = _parse_expiry_param(request.args.get("expiry"))
    expiry = expiry_override if expiry_override is not None else cc_lib.get_target_monthly_expiry()[0]

    try:
        result = cc_lib.get_monthly_expiry_ce_candidates(
            kite_client=kite_client,
            symbol=symbol,
            ltp=ltp,
            lot_size=lot_size,
            expiry=expiry,
            otm_pct=otm_pct,
            min_premium_per_lot=min_premium,
        )
    except Exception:
        logger.exception("/api/covered_calls/candidates failed for %s", symbol)
        return jsonify({"error": "Failed to fetch candidates"}), 500

    return jsonify({"symbol": symbol, "ltp": ltp, "candidates": [cc_lib._candidate_to_dict(c) for c in result]})


@covered_calls_bp.route("/api/place", methods=["POST"])
def place_order() -> Any:
    """Place a covered call SELL CE order.

    Request body (JSON):
        tradingsymbol (str): NFO option tradingsymbol.
        quantity (int): Number of shares (lots × lot_size).
        limit_price (float): Limit price for the sell.
        underlying_symbol (str): Underlying stock symbol for tagging.

    Returns:
        JSON with order_id on success.
    """
    kite_client = _resolve_kite_client()
    if kite_client is None:
        return jsonify({"error": "Not authenticated"}), 401

    payload: dict[str, Any] = request.get_json(force=True) or {}

    tradingsymbol: str = payload.get("tradingsymbol", "").strip()
    underlying_symbol: str = payload.get("underlying_symbol", "").strip()
    try:
        quantity = int(payload["quantity"])
        limit_price = float(payload["limit_price"])
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify({"error": f"Invalid payload: {exc}"}), 400

    if not tradingsymbol:
        return jsonify({"error": "tradingsymbol is required"}), 400
    if quantity <= 0:
        return jsonify({"error": "quantity must be positive"}), 400
    if limit_price <= 0:
        return jsonify({"error": "limit_price must be positive"}), 400

    # Optional fields from the confirm modal (used for pending-order persistence)
    strike: float = float(payload.get("strike", 0) or 0)
    expiry: str | None = payload.get("expiry") or None
    lots: int = int(payload.get("lots", 1) or 1)
    lot_size: int = int(payload.get("lot_size", 1) or 1)

    try:
        result = cc_lib.place_covered_call(
            kite_client=kite_client,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            limit_price=limit_price,
            underlying_symbol=underlying_symbol or tradingsymbol,
        )
    except Exception:
        logger.exception("place_covered_call failed for %s", tradingsymbol)
        return jsonify({"error": "Order placement failed — check logs"}), 500

    order_id: str = result["order_id"]

    # Persist so refresh shows "Pending Fill" until Kite confirms the fill
    try:
        from common_lib import get_ist_now
        placed_at = get_ist_now().isoformat()
    except Exception:
        from datetime import datetime
        placed_at = datetime.now().isoformat()

    try:
        pending_orders_db.save_pending_order(
            order_id=order_id,
            symbol=underlying_symbol or tradingsymbol,
            tradingsymbol=tradingsymbol,
            strike=strike,
            expiry=expiry,
            quantity=quantity,
            lots=lots,
            lot_size=lot_size,
            limit_price=limit_price,
            placed_at=placed_at,
        )
    except Exception:
        logger.exception("save_pending_order failed for %s — order was still placed", order_id)

    return jsonify({"ok": True, "order_id": order_id})


@covered_calls_bp.route("/debug")
def debug_page() -> str:
    """Render the holdings debug page."""
    return render_template("covered_calls/debug.html")


@covered_calls_bp.route("/api/debug_holdings")
def debug_holdings() -> Any:
    """Diagnostic endpoint: return all holdings with F&O lot size lookup and exclusion reason.

    Returns both a raw per-entry view (one row per product type, e.g. CNC + MTF separately)
    and an aggregated view (combined totals per symbol, matching dashboard logic).

    Returns:
        JSON with:
          raw_entries   — every holding entry as Kite returned it (product visible)
          aggregated    — per-symbol combined totals after CNC+MTF merge
          summary       — total / included / excluded counts (aggregated basis)
    """
    kite_client = _resolve_kite_client()
    if kite_client is None:
        return jsonify({"error": "Not authenticated"}), 401

    try:
        raw_holdings = kite_client.holdings()
    except Exception:
        logger.exception("/api/debug_holdings: holdings() failed")
        return jsonify({"error": "Failed to fetch holdings"}), 500

    # --- raw per-entry view (unchanged, useful for debugging Zerodha API response) ---
    raw_result = []
    for h in raw_holdings:
        symbol = h.get("tradingsymbol", "")
        exchange = h.get("exchange", "")
        product = h.get("product", "")
        quantity = int(h.get("quantity", 0))
        t1_qty = int(h.get("t1_quantity", 0))
        pledged_qty = int(h.get("used_quantity", 0))
        collateral_qty = int(h.get("collateral_quantity", 0))
        settled_qty = quantity + pledged_qty + collateral_qty - t1_qty

        lot_size = cc_lib._get_lot_size_for_symbol(symbol) if symbol else None
        coverage_pct = round((settled_qty / lot_size) * 100, 1) if lot_size and settled_qty > 0 else 0.0
        eligible_lots = (settled_qty // lot_size) if (lot_size and settled_qty > 0) else 0

        raw_result.append({
            "symbol": symbol,
            "product": product,
            "exchange": exchange,
            "quantity": quantity,
            "pledged_qty": pledged_qty,
            "collateral_qty": collateral_qty,
            "t1_quantity": t1_qty,
            "settled_qty": settled_qty,
            "lot_size": lot_size,
            "coverage_pct": coverage_pct,
            "eligible_lots": eligible_lots,
            "ltp": float(h.get("last_price", 0) or 0),
            "avg_price": float(h.get("average_price", 0) or 0),
        })
    raw_result.sort(key=lambda x: (x["symbol"], x["product"]))

    # --- aggregated view (mirrors get_eligible_holdings logic) ---
    buckets = cc_lib._aggregate_holdings_by_symbol(raw_holdings)
    agg_result = []
    for symbol, acc in sorted(buckets.items()):
        lot_size = cc_lib._get_lot_size_for_symbol(symbol)
        settled_qty = acc.settled_qty
        coverage_pct = round((settled_qty / lot_size) * 100, 1) if lot_size and settled_qty > 0 else 0.0
        eligible_lots = (settled_qty // lot_size) if (lot_size and settled_qty > 0) else 0
        avg_price = round(acc.total_cost / settled_qty, 4) if settled_qty else 0.0

        if exchange not in ("NSE", "BSE"):
            reason = f"exchange={acc.exchange} not NSE/BSE"
        elif settled_qty <= 0:
            reason = f"settled_qty={settled_qty} — all entries had zero usable qty"
        elif lot_size is None or lot_size == 0:
            reason = "no F&O lot size in instruments.db"
        elif settled_qty < (lot_size * 0.70):
            reason = f"only {coverage_pct}% of lot_size={lot_size} ({settled_qty} shares)"
        else:
            reason = "included"

        agg_result.append({
            "symbol": symbol,
            "products": "+".join(sorted(acc.products)),
            "exchange": acc.exchange,
            "settled_qty": settled_qty,
            "lot_size": lot_size,
            "coverage_pct": coverage_pct,
            "eligible_lots": eligible_lots,
            "ltp": acc.ltp,
            "avg_price": avg_price,
            "unrealised_pnl": round(acc.unrealised_pnl, 2),
            "excluded": reason != "included",
            "exclusion_reason": reason,
        })

    return jsonify({
        "raw_entries": raw_result,
        "aggregated": agg_result,
        "summary": {
            "total_raw_entries": len(raw_result),
            "total_symbols": len(agg_result),
            "included": sum(1 for r in agg_result if not r["excluded"]),
            "excluded": sum(1 for r in agg_result if r["excluded"]),
        },
    })


@covered_calls_bp.route("/api/config", methods=["GET", "POST"])
def config_endpoint() -> Any:
    """Read or update the covered calls config.

    GET: Returns current config.
    POST body (JSON):
        otm_pct (float, optional)
        min_premium_per_lot (float, optional)
        per_symbol (dict, optional): Per-symbol OTM override map.

    Returns:
        JSON config dict.
    """
    if request.method == "GET":
        return jsonify(_load_config())

    payload: dict[str, Any] = request.get_json(force=True) or {}
    config = _load_config()

    if "otm_pct" in payload:
        config["otm_pct"] = float(payload["otm_pct"])
    if "min_premium_per_lot" in payload:
        config["min_premium_per_lot"] = float(payload["min_premium_per_lot"])
    if "per_symbol" in payload and isinstance(payload["per_symbol"], dict):
        config["per_symbol"] = payload["per_symbol"]

    try:
        _save_config(config)
    except OSError:
        logger.exception("Failed to save covered_calls_config.json")
        return jsonify({"error": "Failed to persist config"}), 500

    return jsonify(config)
