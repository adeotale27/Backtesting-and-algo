"""
Early Exit GTT Tool — SENSEX edition.

Business logic library for placing early-exit GTT BUY orders on SENSEX
(BFO exchange) short option positions.

Flow mirrors early_exit_lib.py for NIFTY, with these key differences:
  - Underlying spot:   BSE:SENSEX
  - Options/futures:   BFO exchange
  - Basis deduction:   NIFTY empirical table × dynamic (SENSEX_LTP / NIFTY_LTP) multiplier
  - Lot size:          fetched from BFO-FUT row in instrument_cache
  - BSE historical:    tries 3:27 PM minute candle; falls back to kite.quote()
  - Discount %:        configurable per-preview (default 10%); also editable per-row in UI

Endpoints (flask_app.py):
  GET  /early-exit-sensex/preview   — compute and return preview data (no orders placed)
  POST /early-exit-sensex/run       — place GTT BUY orders for the confirmed symbol list
  GET  /early-exit-sensex/expiries  — list expiry dates with open short SENSEX positions
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import greeks_lib as _mib

import instrument_cache
from common_lib import IST, get_ist_now
# Re-use shared helpers from the NIFTY library (basis interpolation, tick rounding, candle fetch)
from early_exit_lib import (
    _interpolate_basis,
    _get_prev_trading_day,
    _get_prev_trading_day_with_candle,
    _round_to_tick,
    _get_candle_close_at,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTEREST_RATE_DEFAULT = 10.0   # % — overridden by configfile.ini [option_details]
MIN_EXIT_LOTS = 5
MIN_TICK = 0.05
DEFAULT_DISCOUNT_PCT = 10.0    # GTT price = fair_value × (1 − discount/100)

# ---------------------------------------------------------------------------
# Dynamic SENSEX/NIFTY multiplier for basis scaling
# ---------------------------------------------------------------------------


def _get_basis_multiplier(kite) -> float:
    """Compute live SENSEX/NIFTY ratio to scale the NIFTY basis-deduction table.

    The NIFTY empirical basis table (30–160 pts) is scaled by this ratio so
    the same relative basis applies to SENSEX's larger absolute price level.
    Falls back to the hardcoded approximate ratio if LTP fetch fails.
    """
    try:
        resp = kite.ltp(["BSE:SENSEX", "NSE:NIFTY 50"])
        sensex = resp["BSE:SENSEX"]["last_price"]
        nifty  = resp["NSE:NIFTY 50"]["last_price"]
        if nifty and nifty > 0:
            return float(sensex) / float(nifty)
    except Exception as exc:
        logger.warning("Could not fetch SENSEX/NIFTY LTP for multiplier: %s", exc)
    return 77500.0 / 23900.0   # hardcoded fallback ≈ 3.24


# ---------------------------------------------------------------------------
# SENSEX price estimation
# ---------------------------------------------------------------------------


def get_estimated_sensex(kite, now_ist: datetime) -> Dict[str, Any]:
    """Return estimated SENSEX underlying price and related metadata.

    Pre-open (before 9:15 AM IST):
        multiplier = live SENSEX / live NIFTY LTP
        scaled_basis = _interpolate_basis(futures_dte) × multiplier
        sensex_from_futures = BFO futures LTP − scaled_basis
        estimated_sensex = average(sensex_from_futures, spot_ltp)

    Live (9:15 AM+):
        Average of last ≤5 completed 1-min BSE:SENSEX candles.
        Falls back to kite.ltp() if historical candles are unavailable.

    Returns a dict with keys:
        mode, estimated_sensex, sensex_from_futures, sensex_spot,
        futures_symbol, futures_ltp, futures_dte, basis_deduction,
        basis_multiplier, warnings.
    Raises RuntimeError on unrecoverable failures.
    """
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    is_preopen  = now_ist < market_open
    warnings: List[str] = []

    if is_preopen:
        multiplier = _get_basis_multiplier(kite)

        fut = instrument_cache.get_near_month_sensex_future()
        if not fut:
            raise RuntimeError(
                "SENSEX futures not found in instrument cache — run a sync first"
            )

        fut_symbol    = fut["tradingsymbol"]
        fut_expiry_str = fut["expiry"]
        fut_expiry = (date.fromisoformat(fut_expiry_str)
                      if isinstance(fut_expiry_str, str) else fut_expiry_str)
        dte   = (fut_expiry - now_ist.date()).days
        basis = _interpolate_basis(dte) * multiplier

        ltp_data    = kite.ltp([f"BFO:{fut_symbol}"])
        futures_ltp = ltp_data[f"BFO:{fut_symbol}"]["last_price"]
        if futures_ltp == 0:
            raise RuntimeError(
                "SENSEX futures LTP is 0 — pre-open data not available yet"
            )

        sensex_from_futures = futures_ltp - basis

        spot_data = kite.ltp(["BSE:SENSEX"])
        spot_ltp  = spot_data["BSE:SENSEX"]["last_price"]
        if spot_ltp == 0:
            warnings.append(
                "SENSEX spot LTP is 0 — pre-open may not have started; "
                "using futures estimate only"
            )
            spot_ltp = sensex_from_futures

        estimated_sensex = (sensex_from_futures + spot_ltp) / 2

        return {
            "mode":                "preopen",
            "estimated_sensex":    round(estimated_sensex, 2),
            "sensex_from_futures": round(sensex_from_futures, 2),
            "sensex_spot":         round(spot_ltp, 2),
            "futures_symbol":      fut_symbol,
            "futures_ltp":         round(futures_ltp, 2),
            "futures_dte":         dte,
            "basis_deduction":     round(basis, 2),
            "basis_multiplier":    round(multiplier, 4),
            "warnings":            warnings,
        }

    else:
        # Live mode: average of last ≤5 completed 1-min BSE:SENSEX candles
        spot_resp    = kite.ltp(["BSE:SENSEX"])
        spot_ltp     = float(spot_resp["BSE:SENSEX"]["last_price"])
        sensex_token = spot_resp["BSE:SENSEX"]["instrument_token"]

        estimated_sensex = spot_ltp   # fallback used if candles unavailable

        try:
            to_dt   = now_ist.replace(second=0, microsecond=0)
            from_dt = to_dt - timedelta(minutes=7)
            candles = kite.historical_data(sensex_token, from_dt, to_dt, "minute")
            recent  = candles[-5:] if len(candles) >= 5 else candles
            if recent:
                estimated_sensex = sum(c["close"] for c in recent) / len(recent)
            else:
                warnings.append(
                    "No BSE:SENSEX 1-min candles available; using spot LTP"
                )
        except Exception as exc:
            warnings.append(
                f"BSE:SENSEX historical data unavailable ({exc}); using spot LTP"
            )

        return {
            "mode":                "live",
            "estimated_sensex":    round(estimated_sensex, 2),
            "sensex_from_futures": None,
            "sensex_spot":         round(spot_ltp, 2),
            "futures_symbol":      None,
            "futures_ltp":         None,
            "futures_dte":         None,
            "basis_deduction":     None,
            "basis_multiplier":    None,
            "warnings":            warnings,
        }


# ---------------------------------------------------------------------------
# Position filtering
# ---------------------------------------------------------------------------


def _enrich_sensex_shorts(kite) -> List[Dict[str, Any]]:
    """Fetch and enrich all short SENSEX option positions (all expiries)."""
    positions = kite.positions()["net"]

    sensex_shorts = [
        p for p in positions
        if (p["tradingsymbol"].startswith("SENSEX")
            and (p["tradingsymbol"].endswith("CE") or p["tradingsymbol"].endswith("PE"))
            and p.get("quantity", 0) < 0)
    ]

    enriched = []
    for pos in sensex_shorts:
        inst = instrument_cache.get_instrument(pos["tradingsymbol"])
        if inst and inst.get("expiry"):
            expiry_str  = inst["expiry"]
            expiry_date = (date.fromisoformat(expiry_str)
                           if isinstance(expiry_str, str) else expiry_str)
            p = dict(pos)
            p["_expiry_date"]      = expiry_date
            p["_strike"]           = float(inst.get("strike") or 0)
            p["_instrument_type"]  = inst.get("instrument_type", "")
            p["_instrument_token"] = inst.get("instrument_token")
            p["_segment"]          = inst.get("segment")
            enriched.append(p)

    return enriched


def get_available_expiries(kite) -> List[date]:
    """Return sorted list of unique expiry dates with open short SENSEX positions."""
    enriched = _enrich_sensex_shorts(kite)
    if not enriched:
        return []
    return sorted(set(p["_expiry_date"] for p in enriched))


def get_target_positions(kite, expiry: Optional[date] = None) -> List[Dict[str, Any]]:
    """Return enriched short SENSEX option positions for the specified expiry.

    If expiry is None: returns positions for the nearest expiry (no fall-through
    needed since we show an expiry dropdown in the UI).
    """
    enriched = _enrich_sensex_shorts(kite)
    if not enriched:
        return []
    if expiry is not None:
        return [p for p in enriched if p["_expiry_date"] == expiry]
    nearest_expiry = min(p["_expiry_date"] for p in enriched)
    return [p for p in enriched if p["_expiry_date"] == nearest_expiry]


# ---------------------------------------------------------------------------
# Per-leg computation (IV back-solve + re-price)
# ---------------------------------------------------------------------------


def compute_leg(kite, pos: Dict[str, Any], estimated_sensex: float,
                prev_sensex_close: float, prev_day: date,
                lot_size: int, interest_rate: float,
                now_ist: datetime,
                discount_pct: float = DEFAULT_DISCOUNT_PCT,
                iv_offset_pct: float = 0.0) -> Dict[str, Any]:
    """Compute IV, fair value, GTT price and exit quantity for one SENSEX leg.

    discount_pct:  percentage below effective fair value for GTT (e.g. 10.0 → 10%).
    iv_offset_pct: global IV shift in percentage points applied before re-pricing
                   (e.g. +1.0 raises IV by 1pp → higher fair value).

    Returns a leg dict. Sets 'error' key (non-None) if any step fails.
    """
    sym         = pos["tradingsymbol"]
    strike      = pos["_strike"]
    inst_type   = pos["_instrument_type"]    # 'CE' or 'PE'
    expiry_date = pos["_expiry_date"]
    net_qty     = pos.get("quantity", 0)
    inst_token  = pos["_instrument_token"]

    leg: Dict[str, Any] = {
        "symbol":               sym,
        "expiry":               str(expiry_date),
        "net_quantity":         net_qty,
        "strike":               strike,
        "option_type":          inst_type,
        "prev_close_option":    None,
        "prev_close_sensex":    round(prev_sensex_close, 2) if prev_sensex_close else None,
        "iv_percent":           None,
        "fair_value":           None,
        "current_ltp":          None,
        "effective_fair_value": None,
        "discount_pct":         round(discount_pct, 2),
        "gtt_price":            None,
        "exit_qty":             None,
        "error":                None,
        "instrument_token":     pos.get("_instrument_token"),
        "segment":              pos.get("_segment"),
    }

    # 1. Option price at 3:27 PM yesterday (BFO option historical data via token)
    prev_opt = _get_candle_close_at(kite, inst_token, prev_day, 15, 27)
    if prev_opt is None or prev_opt <= 0:
        leg["error"] = f"No 3:27 PM candle data for {sym} on {prev_day}"
        return leg
    leg["prev_close_option"] = round(prev_opt, 2)

    # 2. Time to expiry at yesterday's market close (3:30 PM IST)
    yesterday_close_dt = datetime(prev_day.year, prev_day.month, prev_day.day,
                                  15, 30, tzinfo=IST)
    expiry_dt = datetime(expiry_date.year, expiry_date.month, expiry_date.day,
                         15, 30, tzinfo=IST)
    dte_for_iv = (expiry_dt - yesterday_close_dt).total_seconds() / 86400.0
    if dte_for_iv <= 0:
        leg["error"] = "Option already expired"
        return leg

    # 3. Back-solve IV using Black-Scholes (mibian)
    try:
        bs_args = [prev_sensex_close, strike, interest_rate, dte_for_iv]
        bs = (_mib.BS(bs_args, callPrice=prev_opt)
              if inst_type == "CE"
              else _mib.BS(bs_args, putPrice=prev_opt))
        iv = bs.impliedVolatility
        if iv is None or iv <= 0:
            leg["error"] = (
                "IV calculation returned no result "
                "(option may be too far OTM or price too low)"
            )
            return leg
    except Exception as exc:
        leg["error"] = f"IV calculation failed: {exc}"
        return leg

    # Apply user-requested IV offset (± percentage points) before re-pricing
    if iv_offset_pct:
        iv += iv_offset_pct
        if iv <= 0:
            leg["error"] = f"IV after offset ({iv_offset_pct:+.1f}%) is non-positive — reduce offset"
            return leg
    leg["iv_percent"] = round(iv, 2)

    # 4. Re-price using estimated SENSEX and current time-to-expiry
    dte_now = (expiry_dt - now_ist).total_seconds() / 86400.0
    if dte_now <= 0:
        leg["error"] = "Option expires before current time"
        return leg
    try:
        bs_now     = _mib.BS([estimated_sensex, strike, interest_rate, dte_now],
                             volatility=iv)
        fair_value = float(bs_now.callPrice if inst_type == "CE" else bs_now.putPrice)
        fair_value = max(fair_value, MIN_TICK)
    except Exception as exc:
        leg["error"] = f"Fair value calculation failed: {exc}"
        return leg
    leg["fair_value"] = round(fair_value, 2)

    # 5. Fetch current option market LTP; use whichever is lower (fair or market)
    current_ltp = fair_value   # safe fallback if LTP unavailable
    try:
        ltp_resp = kite.ltp([f"BFO:{sym}"])
        mkt_ltp  = float(ltp_resp[f"BFO:{sym}"]["last_price"] or 0)
        if mkt_ltp > 0:
            current_ltp = mkt_ltp
    except Exception as exc:
        logger.warning("Could not fetch current LTP for %s: %s", sym, exc)
    leg["current_ltp"] = round(current_ltp, 2)

    effective_fair = min(fair_value, current_ltp)
    leg["effective_fair_value"] = round(effective_fair, 2)

    # 6. GTT price: discount applied to effective fair value, rounded to ₹0.05 tick
    leg["gtt_price"] = max(
        _round_to_tick(effective_fair * (1.0 - discount_pct / 100.0)),
        MIN_TICK,
    )

    # 7. Exit quantity: max(5 lots, open_lots // 5); close everything if < 5 lots
    open_lots = abs(net_qty) // lot_size
    exit_lots = open_lots if open_lots < MIN_EXIT_LOTS else max(MIN_EXIT_LOTS, open_lots // 5)
    leg["exit_qty"] = exit_lots * lot_size

    return leg


# ---------------------------------------------------------------------------
# Preview (full computation pipeline — no orders placed)
# ---------------------------------------------------------------------------


def build_preview(kite, interest_rate: float = INTEREST_RATE_DEFAULT,
                  expiry: Optional[date] = None,
                  discount_pct: float = DEFAULT_DISCOUNT_PCT,
                  iv_offset_pct: float = 0.0) -> Dict[str, Any]:
    """Run the full SENSEX early-exit computation and return a preview dict.

    Called by GET /early-exit-sensex/preview. No GTT orders are placed.
    discount_pct:  GTT discount below effective fair value (default 10%).
    iv_offset_pct: global IV shift in percentage points (default 0 = no shift).
    """
    now_ist  = get_ist_now()
    warnings: List[str] = []

    # Step 1: Estimate SENSEX price
    sensex_info = get_estimated_sensex(kite, now_ist)
    warnings.extend(sensex_info.pop("warnings", []))
    estimated_sensex = sensex_info["estimated_sensex"]

    lot_size = instrument_cache.get_sensex_lot_size()
    if lot_size <= 0:
        lot_size = 20

    result = {
        "ist_time":      now_ist.strftime("%H:%M:%S"),
        "lot_size":      lot_size,
        "discount_pct":  round(discount_pct, 2),
        "iv_offset_pct": round(iv_offset_pct, 2),
        "selected_expiry": str(expiry) if expiry else None,
        **sensex_info,
        "warnings":      warnings,
        "legs":          [],
    }

    # Step 2: Qualifying positions
    target_positions = get_target_positions(kite, expiry=expiry)
    if not target_positions:
        return result

    # Step 3: Previous trading day & SENSEX closing price.
    # Walk backwards through weekends *and* exchange holidays until we find a
    # day that actually has candle data, so a BSE holiday never causes a miss.
    sensex_token = None
    try:
        _sq = kite.ltp(["BSE:SENSEX"])
        sensex_token = _sq["BSE:SENSEX"]["instrument_token"]
    except Exception:
        pass

    prev_day      = None
    prev_sensex_close = None
    if sensex_token:
        prev_day, prev_sensex_close = _get_prev_trading_day_with_candle(
            kite, sensex_token, now_ist.date(), 15, 27
        )

    simple_prev = _get_prev_trading_day(now_ist.date())

    if prev_day is None or prev_sensex_close is None:
        result["warnings"].append(
            "BSE:SENSEX 3:27 PM candle unavailable for the last 10 days; "
            "using spot LTP as fallback."
        )
        prev_day = simple_prev
        try:
            sq = kite.ltp(["BSE:SENSEX"])
            prev_sensex_close = float(sq["BSE:SENSEX"]["last_price"])
        except Exception as exc:
            raise RuntimeError(f"Cannot fetch BSE:SENSEX price: {exc}") from exc
    elif prev_day != simple_prev:
        result["warnings"].append(
            f"BSE:SENSEX 3:27 PM candle unavailable for {simple_prev} "
            f"(exchange holiday); using {prev_day} instead."
        )

    # Step 4: Compute each leg
    result["legs"] = [
        compute_leg(kite, pos, estimated_sensex, prev_sensex_close,
                    prev_day, lot_size, interest_rate, now_ist,
                    discount_pct=discount_pct, iv_offset_pct=iv_offset_pct)
        for pos in target_positions
    ]
    return result


# ---------------------------------------------------------------------------
# GTT placement
# ---------------------------------------------------------------------------


def place_gtts(kite, symbols: List[str], legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Place single-leg GTT BUY orders on BFO for the selected SENSEX symbols.

    `legs` is the list from build_preview() (or user-edited version from the UI).
    The gtt_price and exit_qty used are exactly what was shown/edited in preview.
    """
    leg_map = {leg["symbol"]: leg for leg in legs}
    results = []

    for sym in symbols:
        leg = leg_map.get(sym)
        if not leg:
            results.append({
                "symbol": sym, "status": "error", "gtt_id": None,
                "gtt_price": None, "exit_qty": None,
                "error": "Leg data missing from request",
            })
            continue

        if leg.get("error"):
            results.append({
                "symbol": sym, "status": "skipped", "gtt_id": None,
                "gtt_price": leg.get("gtt_price"),
                "exit_qty":  leg.get("exit_qty"),
                "error":     leg["error"],
            })
            continue

        gtt_price = float(leg["gtt_price"])
        exit_qty  = int(leg["exit_qty"])

        # Kite GTT requires last_price (current LTP of the instrument)
        try:
            ltp_resp   = kite.ltp([f"BFO:{sym}"])
            last_price = float(ltp_resp[f"BFO:{sym}"]["last_price"] or gtt_price)
        except Exception:
            last_price = gtt_price

        try:
            order = [{
                "exchange":         "BFO",
                "tradingsymbol":    sym,
                "transaction_type": kite.TRANSACTION_TYPE_BUY,
                "quantity":         exit_qty,
                "order_type":       kite.ORDER_TYPE_LIMIT,
                "product":          kite.PRODUCT_NRML,
                "price":            gtt_price,
            }]
            gtt_resp = kite.place_gtt(
                trigger_type   = kite.GTT_TYPE_SINGLE,
                tradingsymbol  = sym,
                exchange       = "BFO",
                trigger_values = [gtt_price],
                last_price     = last_price,
                orders         = order,
            )
            results.append({
                "symbol":    sym,
                "status":    "placed",
                "gtt_id":    gtt_resp.get("trigger_id"),
                "gtt_price": gtt_price,
                "exit_qty":  exit_qty,
                "error":     None,
            })
        except Exception as exc:
            results.append({
                "symbol":    sym,
                "status":    "error",
                "gtt_id":    None,
                "gtt_price": gtt_price,
                "exit_qty":  exit_qty,
                "error":     str(exc),
            })

    return results


def place_active_orders(
    kite, symbols: List[str], legs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Place regular LIMIT BUY orders on BFO for the selected SENSEX symbols.

    Uses the same gtt_price and exit_qty from the preview, placed directly
    into the active order book (not a GTT trigger). Intended for use when
    the market is live.

    Args:
        kite: Authenticated KiteConnect instance.
        symbols: List of trading symbols to place orders for.
        legs: Leg dicts from build_preview() (may have user-edited prices/qty).

    Returns:
        List of result dicts with keys: symbol, status, order_id, price, exit_qty, error.
    """
    leg_map = {leg["symbol"]: leg for leg in legs}
    results: List[Dict[str, Any]] = []

    for sym in symbols:
        leg = leg_map.get(sym)
        if not leg:
            results.append({"symbol": sym, "status": "error", "order_id": None,
                            "price": None, "exit_qty": None,
                            "error": "Leg data missing from request"})
            continue

        if leg.get("error"):
            results.append({"symbol": sym, "status": "skipped", "order_id": None,
                            "price": leg.get("gtt_price"),
                            "exit_qty": leg.get("exit_qty"),
                            "error": leg["error"]})
            continue

        limit_price = float(leg["gtt_price"])
        exit_qty    = int(leg["exit_qty"])

        try:
            order_id = kite.place_order(
                variety          = kite.VARIETY_REGULAR,
                exchange         = "BFO",
                tradingsymbol    = sym,
                transaction_type = kite.TRANSACTION_TYPE_BUY,
                quantity         = exit_qty,
                product          = kite.PRODUCT_NRML,
                order_type       = kite.ORDER_TYPE_LIMIT,
                price            = limit_price,
            )
            logging.info(
                "Active order placed (SENSEX): %s qty=%d price=%.2f order_id=%s",
                sym, exit_qty, limit_price, order_id,
            )
            results.append({
                "symbol":   sym, "status": "placed",
                "order_id": order_id,
                "price":    limit_price, "exit_qty": exit_qty, "error": None,
            })
        except Exception as exc:
            logging.exception("Active order failed (SENSEX) for %s", sym)
            results.append({
                "symbol":   sym, "status": "error",
                "order_id": None, "price": limit_price,
                "exit_qty": exit_qty, "error": str(exc),
            })

    return results
