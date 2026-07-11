"""
Early Exit GTT Tool — business logic library.

Handles all computation and Kite API interactions for the early-exit tool.
Flask routes in flask_app.py call into this module, keeping routes thin.

Flow:
  1. Estimate NIFTY fair price at open (pre-open: futures+spot avg; live: 5-candle avg)
  2. For each short NIFTY option at the nearest expiry, back-solve IV from the
     previous session's 3:27 PM candle using Black-Scholes (mibian).
  3. Re-price the option using the estimated NIFTY and current time-to-expiry.
  4. Compute GTT price = fair_value × 0.90, rounded to ₹0.05 tick.
  5. Compute exit quantity: max(5 lots, open_lots // 5); close everything if < 5 lots.
  6. Place single-leg GTT BUY orders on NFO.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import greeks_lib as _mib

import instrument_cache
from common_lib import IST, get_ist_now

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTEREST_RATE_DEFAULT = 10.0   # % — overridden by configfile.ini [option_details]
MIN_EXIT_LOTS = 5
MIN_TICK = 0.05
DEFAULT_DISCOUNT_PCT = 10.0    # GTT price = effective_fair × (1 − discount/100)

# ---------------------------------------------------------------------------
# Basis interpolation (empirical, hardcoded)
# ---------------------------------------------------------------------------


def _interpolate_basis(dte: float) -> float:
    """Return the linearly interpolated NIFTY futures basis deduction (in points).

    Breakpoints (days-to-expiry of the futures contract → deduction):
        >28d: 160,  21–28d: 130–160,  14–21d: 100–130,
         7–14d: 80–100,  3–7d: 50–80,  <3d: 30
    """
    if dte >= 28:
        return 160.0
    elif dte >= 21:
        return 130.0 + (dte - 21) / 7.0 * 30.0
    elif dte >= 14:
        return 100.0 + (dte - 14) / 7.0 * 30.0
    elif dte >= 7:
        return 80.0 + (dte - 7) / 7.0 * 20.0
    elif dte >= 3:
        return 50.0 + (dte - 3) / 4.0 * 30.0
    return 30.0


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _get_prev_trading_day(d: date) -> date:
    """Return the previous weekday (skips Sat/Sun; no public-holiday calendar).

    Note: Use _get_prev_trading_day_with_candle() when you also need to skip
    exchange holidays where no candle data exists.
    """
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def _round_to_tick(price: float, tick: float = MIN_TICK) -> float:
    """Round price down to the nearest NSE tick size (₹0.05)."""
    return round(round(price / tick) * tick, 2)


def _get_candle_close_at(kite, instrument_token: int, d: date,
                          hour: int, minute: int) -> Optional[float]:
    """Fetch the close of a specific 1-minute candle. Returns None on any failure."""
    from_dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)
    to_dt   = datetime(d.year, d.month, d.day, hour, minute + 1, tzinfo=IST)
    try:
        candles = kite.historical_data(instrument_token, from_dt, to_dt, 'minute')
        if candles:
            return float(candles[0]['close'])
    except Exception as exc:
        logger.warning("Candle fetch failed token=%s %02d:%02d %s: %s",
                       instrument_token, hour, minute, d, exc)
    return None


def _get_prev_trading_day_with_candle(
    kite,
    instrument_token: int,
    from_date: date,
    hour: int,
    minute: int,
    max_lookback: int = 10,
) -> tuple:
    """Walk backwards from from_date to find the most recent day with candle data.

    Skips both weekends and exchange holidays (days where the candle returns
    no data).  This handles BSE/NSE public holidays automatically without
    needing a hardcoded holiday calendar.

    Args:
        kite:             Authenticated KiteConnect instance.
        instrument_token: Instrument token to query historical data for.
        from_date:        Start date to walk back from (exclusive).
        hour:             Hour of the candle to check (IST).
        minute:           Minute of the candle to check (IST).
        max_lookback:     Maximum number of calendar days to look back.

    Returns:
        (trading_day, candle_close) — both None if no data found within
        max_lookback days.
    """
    candidate = from_date - timedelta(days=1)
    attempts = 0
    while attempts < max_lookback:
        if candidate.weekday() >= 5:   # skip Saturday (5) and Sunday (6)
            candidate -= timedelta(days=1)
            continue
        close = _get_candle_close_at(kite, instrument_token, candidate, hour, minute)
        if close is not None and close > 0:
            return candidate, close
        logger.info(
            "No %02d:%02d candle data for token=%s on %s (holiday?); "
            "trying previous day.",
            hour, minute, instrument_token, candidate,
        )
        candidate -= timedelta(days=1)
        attempts += 1
    logger.warning(
        "Could not find a trading day with candle data within %d days of %s.",
        max_lookback, from_date,
    )
    return None, None


# ---------------------------------------------------------------------------
# NIFTY price estimation
# ---------------------------------------------------------------------------


def get_estimated_nifty(kite, now_ist: datetime) -> Dict[str, Any]:
    """Return estimated NIFTY underlying price and related metadata.

    Pre-open (before 9:15 AM IST):
        Average of (futures_ltp − basis) and spot pre-open LTP.

    Live (9:15 AM+):
        Average of the last ≤5 completed 1-minute NIFTY candles.

    Returns a dict with keys:
        mode, estimated_nifty, nifty_from_futures, nifty_spot,
        futures_symbol, futures_ltp, futures_dte, basis_deduction, warnings.
    Raises RuntimeError on unrecoverable failures.
    """
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    is_preopen  = now_ist < market_open
    warnings: List[str] = []

    if is_preopen:
        fut = instrument_cache.get_near_month_nifty_future()
        if not fut:
            raise RuntimeError("NIFTY futures not found in instrument cache — run a sync first")

        fut_symbol    = fut['tradingsymbol']
        fut_expiry_str = fut['expiry']
        fut_expiry = (date.fromisoformat(fut_expiry_str)
                      if isinstance(fut_expiry_str, str) else fut_expiry_str)
        dte   = (fut_expiry - now_ist.date()).days
        basis = _interpolate_basis(dte)

        ltp_data    = kite.ltp([f"NFO:{fut_symbol}"])
        futures_ltp = ltp_data[f"NFO:{fut_symbol}"]['last_price']
        if futures_ltp == 0:
            raise RuntimeError("Futures LTP is 0 — pre-open data not available yet")

        nifty_from_futures = futures_ltp - basis

        spot_data = kite.ltp(["NSE:NIFTY 50"])
        spot_ltp  = spot_data["NSE:NIFTY 50"]['last_price']
        if spot_ltp == 0:
            warnings.append("NIFTY spot LTP is 0 — pre-open may not have started; using futures estimate only")
            spot_ltp = nifty_from_futures

        estimated_nifty = (nifty_from_futures + spot_ltp) / 2

        return {
            "mode":              "preopen",
            "estimated_nifty":   round(estimated_nifty, 2),
            "nifty_from_futures": round(nifty_from_futures, 2),
            "nifty_spot":        round(spot_ltp, 2),
            "futures_symbol":    fut_symbol,
            "futures_ltp":       round(futures_ltp, 2),
            "futures_dte":       dte,
            "basis_deduction":   round(basis, 2),
            "warnings":          warnings,
        }

    else:
        # Live mode: average of last ≤5 completed 1-min candles
        nifty_ltp_resp = kite.ltp(["NSE:NIFTY 50"])
        nifty_token    = nifty_ltp_resp["NSE:NIFTY 50"]['instrument_token']
        to_dt   = now_ist.replace(second=0, microsecond=0)
        from_dt = to_dt - timedelta(minutes=7)
        candles = kite.historical_data(nifty_token, from_dt, to_dt, 'minute')
        recent  = candles[-5:] if len(candles) >= 5 else candles
        if not recent:
            raise RuntimeError("No NIFTY 1-min candles available yet")
        estimated_nifty = sum(c['close'] for c in recent) / len(recent)

        return {
            "mode":            "live",
            "estimated_nifty": round(estimated_nifty, 2),
            "nifty_from_futures": None,
            "nifty_spot":      round(estimated_nifty, 2),
            "futures_symbol":  None,
            "futures_ltp":     None,
            "futures_dte":     None,
            "basis_deduction": None,
            "warnings":        warnings,
        }


# ---------------------------------------------------------------------------
# Position filtering
# ---------------------------------------------------------------------------


def _enrich_nifty_shorts(kite) -> List[Dict[str, Any]]:
    """Fetch and enrich all short NIFTY option positions (all expiries).

    Used internally by both get_available_expiries() and get_target_positions().
    """
    positions = kite.positions()['net']

    nifty_shorts = [
        p for p in positions
        if (p['tradingsymbol'].startswith('NIFTY')
            and not p['tradingsymbol'].startswith('NIFTYIT')
            and (p['tradingsymbol'].endswith('CE') or p['tradingsymbol'].endswith('PE'))
            and p.get('quantity', 0) < 0)
    ]

    enriched = []
    for pos in nifty_shorts:
        inst = instrument_cache.get_instrument(pos['tradingsymbol'])
        if inst and inst.get('expiry'):
            expiry_str  = inst['expiry']
            expiry_date = (date.fromisoformat(expiry_str)
                           if isinstance(expiry_str, str) else expiry_str)
            p = dict(pos)
            p['_expiry_date']      = expiry_date
            p['_strike']           = float(inst.get('strike') or 0)
            p['_instrument_type']  = inst.get('instrument_type', '')
            p['_instrument_token'] = inst.get('instrument_token')
            p['_segment']          = inst.get('segment')
            enriched.append(p)

    return enriched


def get_available_expiries(kite) -> List[date]:
    """Return sorted list of unique expiry dates that have open short NIFTY positions."""
    enriched = _enrich_nifty_shorts(kite)
    if not enriched:
        return []
    return sorted(set(p['_expiry_date'] for p in enriched))


def get_target_positions(kite, expiry: Optional[date] = None) -> List[Dict[str, Any]]:
    """Return enriched short NIFTY option positions for the specified expiry.

    If expiry is None: returns positions for the nearest expiry (falls through
    to next if none exist at the nearest one).
    If expiry is provided: returns positions for that exact expiry only.
    """
    enriched = _enrich_nifty_shorts(kite)

    if not enriched:
        return []

    if expiry is not None:
        return [p for p in enriched if p['_expiry_date'] == expiry]

    # Default: nearest expiry, fall through to next if empty
    nearest_expiry = min(p['_expiry_date'] for p in enriched)
    return [p for p in enriched if p['_expiry_date'] == nearest_expiry]


# ---------------------------------------------------------------------------
# Per-leg computation (IV back-solve + re-price)
# ---------------------------------------------------------------------------


def compute_leg(kite, pos: Dict[str, Any], estimated_nifty: float,
                prev_nifty_close: float, prev_day: date,
                lot_size: int, interest_rate: float,
                now_ist: datetime,
                discount_pct: float = DEFAULT_DISCOUNT_PCT,
                iv_offset_pct: float = 0.0) -> Dict[str, Any]:
    """Compute IV, fair value, GTT price and exit quantity for one position leg.

    discount_pct:  percentage below effective fair value for GTT (e.g. 10.0 → ×0.90).
    iv_offset_pct: global IV shift in percentage points applied before re-pricing
                   (e.g. +1.0 raises IV by 1pp → higher fair value).

    Returns a leg dict. Sets 'error' key (non-None) if any step fails so the
    caller can display an inline error without skipping the row.
    """
    sym         = pos['tradingsymbol']
    strike      = pos['_strike']
    inst_type   = pos['_instrument_type']   # 'CE' or 'PE'
    expiry_date = pos['_expiry_date']
    net_qty     = pos.get('quantity', 0)
    inst_token  = pos['_instrument_token']

    leg: Dict[str, Any] = {
        "symbol":               sym,
        "expiry":               str(expiry_date),
        "net_quantity":         net_qty,
        "strike":               strike,
        "option_type":          inst_type,
        "prev_close_option":    None,
        "prev_close_nifty":     round(prev_nifty_close, 2) if prev_nifty_close else None,
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

    # 1. Option price at 3:27 PM yesterday
    prev_opt = _get_candle_close_at(kite, inst_token, prev_day, 15, 27)
    if prev_opt is None or prev_opt <= 0:
        leg['error'] = f"No 3:27 PM candle data for {sym} on {prev_day}"
        return leg
    leg['prev_close_option'] = round(prev_opt, 2)

    # 2. Time to expiry at yesterday's market close (3:30 PM IST)
    yesterday_close_dt = datetime(prev_day.year, prev_day.month, prev_day.day, 15, 30, tzinfo=IST)
    expiry_dt          = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 15, 30, tzinfo=IST)
    dte_for_iv = (expiry_dt - yesterday_close_dt).total_seconds() / 86400.0
    if dte_for_iv <= 0:
        leg['error'] = "Option already expired"
        return leg

    # 3. Back-solve IV using Black-Scholes
    try:
        bs_args = [prev_nifty_close, strike, interest_rate, dte_for_iv]
        bs = (_mib.BS(bs_args, callPrice=prev_opt)
              if inst_type == 'CE'
              else _mib.BS(bs_args, putPrice=prev_opt))
        iv = bs.impliedVolatility
        if iv is None or iv <= 0:
            leg['error'] = "IV calculation returned no result (option may be too far OTM or price too low)"
            return leg
    except Exception as exc:
        leg['error'] = f"IV calculation failed: {exc}"
        return leg

    # Apply user-requested IV offset (± percentage points) before re-pricing
    if iv_offset_pct:
        iv += iv_offset_pct
        if iv <= 0:
            leg['error'] = f"IV after offset ({iv_offset_pct:+.1f}%) is non-positive — reduce offset"
            return leg
    leg['iv_percent'] = round(iv, 2)

    # 4. Re-price using estimated NIFTY and time from now to expiry
    dte_now = (expiry_dt - now_ist).total_seconds() / 86400.0
    if dte_now <= 0:
        leg['error'] = "Option expires before current time"
        return leg
    try:
        bs_now     = _mib.BS([estimated_nifty, strike, interest_rate, dte_now], volatility=iv)
        fair_value = float(bs_now.callPrice if inst_type == 'CE' else bs_now.putPrice)
        fair_value = max(fair_value, MIN_TICK)
    except Exception as exc:
        leg['error'] = f"Fair value calculation failed: {exc}"
        return leg
    leg['fair_value'] = round(fair_value, 2)

    # 5. Fetch current option market LTP; use whichever is lower (fair or market)
    current_ltp = fair_value   # safe fallback if LTP unavailable
    try:
        ltp_resp = kite.ltp([f"NFO:{sym}"])
        mkt_ltp  = float(ltp_resp[f"NFO:{sym}"]['last_price'] or 0)
        if mkt_ltp > 0:
            current_ltp = mkt_ltp
    except Exception as exc:
        logger.warning("Could not fetch current LTP for %s: %s", sym, exc)
    leg['current_ltp'] = round(current_ltp, 2)

    effective_fair = min(fair_value, current_ltp)
    leg['effective_fair_value'] = round(effective_fair, 2)

    # 6. GTT price: discount applied to effective fair value, rounded to ₹0.05 tick
    leg['gtt_price'] = max(_round_to_tick(effective_fair * (1.0 - discount_pct / 100.0)), MIN_TICK)

    # 7. Exit quantity
    open_lots = abs(net_qty) // lot_size
    exit_lots = open_lots if open_lots < MIN_EXIT_LOTS else max(MIN_EXIT_LOTS, open_lots // 5)
    leg['exit_qty'] = exit_lots * lot_size

    return leg


# ---------------------------------------------------------------------------
# Preview (full computation pipeline — no orders placed)
# ---------------------------------------------------------------------------


def build_preview(kite, interest_rate: float = INTEREST_RATE_DEFAULT,
                  expiry: Optional[date] = None,
                  discount_pct: float = DEFAULT_DISCOUNT_PCT,
                  iv_offset_pct: float = 0.0) -> Dict[str, Any]:
    """Run the full early-exit computation and return a preview dict.

    Called by GET /early-exit/preview. No GTT orders are placed.
    expiry:        if provided, compute only for positions at that specific expiry.
    discount_pct:  GTT discount below effective fair value (default 10%).
    iv_offset_pct: global IV shift in percentage points (default 0 = no shift).
    """
    now_ist  = get_ist_now()
    warnings: List[str] = []

    # Step 1: Estimate NIFTY
    nifty_info = get_estimated_nifty(kite, now_ist)
    warnings.extend(nifty_info.pop('warnings', []))
    estimated_nifty = nifty_info['estimated_nifty']

    # Fetch lot size early so it's always present in the response
    lot_size = instrument_cache.get_lot_size('NIFTY 50')
    if lot_size <= 0:
        lot_size = 65

    result = {
        "ist_time":       now_ist.strftime("%H:%M:%S"),
        "lot_size":       lot_size,
        "discount_pct":   round(discount_pct, 2),
        "iv_offset_pct":  round(iv_offset_pct, 2),
        "selected_expiry": str(expiry) if expiry else None,
        **nifty_info,
        "warnings": warnings,
        "legs": [],
    }

    # Step 2: Qualifying positions for the selected (or nearest) expiry
    target_positions = get_target_positions(kite, expiry=expiry)
    if not target_positions:
        return result

    # Step 3: Supporting data — find the last trading day that actually has
    # candle data (walks back through weekends *and* exchange holidays).
    nifty_quote = kite.ltp(["NSE:NIFTY 50"])
    nifty_token = nifty_quote["NSE:NIFTY 50"]['instrument_token']

    prev_day, prev_nifty_close = _get_prev_trading_day_with_candle(
        kite, nifty_token, now_ist.date(), 15, 27
    )

    if prev_day is None or prev_nifty_close is None:
        result['warnings'].append(
            "NIFTY 3:27 PM candle unavailable for the last 10 days; "
            "using current LTP as fallback."
        )
        prev_day = _get_prev_trading_day(now_ist.date())
        prev_nifty_close = float(nifty_quote["NSE:NIFTY 50"]['last_price'])
    elif prev_day != _get_prev_trading_day(now_ist.date()):
        result['warnings'].append(
            f"NIFTY 3:27 PM candle unavailable for "
            f"{_get_prev_trading_day(now_ist.date())} (exchange holiday); "
            f"using {prev_day} instead."
        )

    # Step 4: Compute each leg
    result['legs'] = [
        compute_leg(kite, pos, estimated_nifty, prev_nifty_close,
                    prev_day, lot_size, interest_rate, now_ist,
                    discount_pct=discount_pct, iv_offset_pct=iv_offset_pct)
        for pos in target_positions
    ]
    return result


# ---------------------------------------------------------------------------
# GTT placement
# ---------------------------------------------------------------------------


def place_gtts(kite, symbols: List[str], legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Place single-leg GTT BUY orders for the selected symbols.

    `legs` is the list previously computed by build_preview() — the GTT price
    and exit quantity used are exactly what was shown in the preview.

    Returns a list of result dicts (one per symbol).
    """
    leg_map = {leg['symbol']: leg for leg in legs}
    results = []

    for sym in symbols:
        leg = leg_map.get(sym)
        if not leg:
            results.append({"symbol": sym, "status": "error", "gtt_id": None,
                            "gtt_price": None, "exit_qty": None,
                            "error": "Leg data missing from request"})
            continue

        if leg.get('error'):
            results.append({"symbol": sym, "status": "skipped", "gtt_id": None,
                            "gtt_price": leg.get('gtt_price'),
                            "exit_qty": leg.get('exit_qty'),
                            "error": leg['error']})
            continue

        gtt_price = float(leg['gtt_price'])
        exit_qty  = int(leg['exit_qty'])

        # Kite GTT requires last_price (current market price of the instrument)
        try:
            ltp_resp   = kite.ltp([f"NFO:{sym}"])
            last_price = float(ltp_resp[f"NFO:{sym}"]['last_price'] or gtt_price)
        except Exception:
            last_price = gtt_price

        try:
            order = [{
                "exchange":         "NFO",
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
                exchange       = "NFO",
                trigger_values = [gtt_price],
                last_price     = last_price,
                orders         = order,
            )
            results.append({
                "symbol": sym, "status": "placed",
                "gtt_id": gtt_resp.get('trigger_id'),
                "gtt_price": gtt_price, "exit_qty": exit_qty, "error": None,
            })
        except Exception as exc:
            results.append({
                "symbol": sym, "status": "error",
                "gtt_id": None, "gtt_price": gtt_price,
                "exit_qty": exit_qty, "error": str(exc),
            })

    return results


def place_active_orders(
    kite, symbols: List[str], legs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Place regular LIMIT BUY orders for the selected symbols.

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
    leg_map = {leg['symbol']: leg for leg in legs}
    results: List[Dict[str, Any]] = []

    for sym in symbols:
        leg = leg_map.get(sym)
        if not leg:
            results.append({"symbol": sym, "status": "error", "order_id": None,
                            "price": None, "exit_qty": None,
                            "error": "Leg data missing from request"})
            continue

        if leg.get('error'):
            results.append({"symbol": sym, "status": "skipped", "order_id": None,
                            "price": leg.get('gtt_price'),
                            "exit_qty": leg.get('exit_qty'),
                            "error": leg['error']})
            continue

        limit_price = float(leg['gtt_price'])
        exit_qty    = int(leg['exit_qty'])

        try:
            order_id = kite.place_order(
                variety          = kite.VARIETY_REGULAR,
                exchange         = "NFO",
                tradingsymbol    = sym,
                transaction_type = kite.TRANSACTION_TYPE_BUY,
                quantity         = exit_qty,
                product          = kite.PRODUCT_NRML,
                order_type       = kite.ORDER_TYPE_LIMIT,
                price            = limit_price,
            )
            logging.info(
                "Active order placed: %s qty=%d price=%.2f order_id=%s",
                sym, exit_qty, limit_price, order_id,
            )
            results.append({
                "symbol": sym, "status": "placed",
                "order_id": order_id,
                "price": limit_price, "exit_qty": exit_qty, "error": None,
            })
        except Exception as exc:
            logging.exception("Active order failed for %s", sym)
            results.append({
                "symbol": sym, "status": "error",
                "order_id": None, "price": limit_price,
                "exit_qty": exit_qty, "error": str(exc),
            })

    return results
