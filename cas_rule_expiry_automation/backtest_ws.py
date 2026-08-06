"""WebSocket-path backtest with CAS detect → sell timestamps.

Replays ticks through TickBus (same contract as live). On CAS close tick:
records cas_detected_at, then simulates ATM±1 CE/PE market sells and
stamps ce_sold_at / pe_sold_at + detect_to_*_ms (wall-clock of the
replay process — measures strategy+order path speed).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from cas_rule_expiry_automation.config import AppConfig, load_config
from cas_rule_expiry_automation.expiry_calendar import INDEX_META, indexes_for_date
from cas_rule_expiry_automation.strike_resolver import otm_strikes
from cas_rule_expiry_automation.timing import TimingEvent, ms_between, new_detect_event
from cas_rule_expiry_automation.ws_stream import TickBus, TickReplay, candle_to_ticks

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class BacktestTrade:
    entry_date: str
    index: str
    close_price: float
    atm: int
    ce_strike: int
    pe_strike: int
    ce_premium: float
    pe_premium: float
    ce_settlement: float
    pe_settlement: float
    lot_size: int
    quantity: int
    pnl: float
    ticks_replayed: int
    trigger: str
    data_source: str = "synthetic"
    premium_source: str = "bs_fallback"
    cas_detected_at: str = ""
    ce_sold_at: str = ""
    pe_sold_at: str = ""
    detect_to_ce_ms: float = 0.0
    detect_to_pe_ms: float = 0.0
    detect_to_done_ms: float = 0.0
    note: str = ""


@dataclass
class BacktestResult:
    strategy: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_pnl: float
    total_return_pct: float
    num_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    max_drawdown_pct: float
    avg_detect_to_done_ms: float = 0.0
    equity_curve: List[Dict[str, object]] = field(default_factory=list)
    trades: List[Dict[str, object]] = field(default_factory=list)
    timings: List[Dict[str, object]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    ws_ticks_total: int = 0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, iv, days, opt_type, rate=0.065) -> float:
    t = max(days, 1e-6) / 365.0
    if spot <= 0 or strike <= 0 or iv <= 0:
        return max(0.0, (spot - strike) if opt_type == "CE" else (strike - spot))
    vol = iv / 100.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if opt_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
    return strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def intrinsic(spot, strike, opt_type) -> float:
    return max(0.0, (spot - strike) if opt_type == "CE" else (strike - spot))


def cas_entry_premium(
    spot: float,
    strike: float,
    opt_type: str,
    strike_gap: int,
    iv: float,
    entry_minutes: float,
    otm_floor: float = 0.0,
) -> float:
    """Fallback premium estimator when live option candles are unavailable.

    Uses Black-Scholes only. ``otm_floor`` is optional and OFF by default
    (set >0 only if you explicitly want a synthetic floor).
    """
    t_days = max(entry_minutes, 1.0) / (60.0 * 24.0)
    bs = bs_price(spot, strike, iv, t_days, opt_type)
    intr = intrinsic(spot, strike, opt_type)
    if otm_floor and otm_floor > 0:
        if opt_type == "CE":
            otm_pts = max(0.0, strike - spot)
        else:
            otm_pts = max(0.0, spot - strike)
        steps = otm_pts / max(strike_gap, 1)
        floor = float(otm_floor) if steps <= 1.5 else float(otm_floor) * math.exp(
            -(steps - 1.5) * 0.9
        )
        if intr > 0:
            return max(bs, intr + max(floor * 0.25, 5.0))
        return max(bs, floor)
    return max(bs, intr)


def _find_option_token(
    kite: Any, index: str, expiry: date, strike: float, opt_type: str
) -> Optional[Dict[str, Any]]:
    meta = INDEX_META[index]
    try:
        instruments = kite.instruments(meta["exchange"])
    except Exception as exc:
        logger.warning("instruments(%s) failed: %s", meta["exchange"], exc)
        return None
    for inst in instruments:
        if (
            str(inst.get("tradingsymbol", "")).startswith(meta["name"])
            and inst.get("expiry") == expiry
            and inst.get("instrument_type") == opt_type
            and abs(float(inst.get("strike") or 0) - float(strike)) < 0.01
        ):
            return inst
    return None


def _option_minute_bars(
    kite: Any, instrument_token: int, d: date
) -> List[dict]:
    try:
        from_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=15, minutes=15)
        to_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=15, minutes=35)
        return list(kite.historical_data(instrument_token, from_dt, to_dt, "minute") or [])
    except Exception as exc:
        logger.warning("option historical failed token=%s: %s", instrument_token, exc)
        return []


def _premium_from_bars(
    bars: List[dict], detect_ts: datetime
) -> tuple[Optional[float], Optional[float], str]:
    """Pick entry premium at/just before CAS detect, exit near end of bars.

    Returns (entry, exit, detail).
    """
    if not bars:
        return None, None, "no_option_bars"
    # Normalize dates
    def _aware(dt):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)

    detect = _aware(detect_ts)
    # Prefer bar whose minute == detect minute; else nearest earlier bar
    ranked = sorted(bars, key=lambda b: abs((_aware(b["date"]) - detect).total_seconds()))
    entry_bar = ranked[0]
    # If detect is mid-collapse, also consider previous minute open (better "as soon as")
    earlier = [
        b for b in bars if _aware(b["date"]) <= detect
    ]
    if earlier:
        entry_bar = earlier[-1]
    entry = float(entry_bar.get("open") or entry_bar.get("close") or 0)
    # Exit = last bar close (post-CAS collapse) 
    exit_px = float(bars[-1].get("close") or 0)
    detail = (
        f"entry@{_aware(entry_bar['date']).strftime('%H:%M')} open={entry:.2f}; "
        f"exit@{_aware(bars[-1]['date']).strftime('%H:%M')} close={exit_px:.2f}"
    )
    return entry, exit_px, detail


def _live_option_pnl_legs(
    kite: Any,
    index: str,
    expiry: date,
    close_px: float,
    ce_strike: int,
    pe_strike: int,
    detect_ts: datetime,
    qty: int,
    iv: float,
    entry_minutes: float,
    strike_gap: int,
) -> Dict[str, Any]:
    """Resolve real CE/PE premiums from Kite; fall back to BS if missing."""
    out: Dict[str, Any] = {
        "ce_premium": None,
        "pe_premium": None,
        "ce_exit": None,
        "pe_exit": None,
        "premium_source": "bs_fallback",
        "detail": "",
    }
    details = []
    for opt, strike, key_p, key_e in (
        ("CE", ce_strike, "ce_premium", "ce_exit"),
        ("PE", pe_strike, "pe_premium", "pe_exit"),
    ):
        inst = _find_option_token(kite, index, expiry, strike, opt)
        if not inst:
            details.append(f"{opt}{strike}:instrument_missing")
            continue
        bars = _option_minute_bars(kite, int(inst["instrument_token"]), expiry)
        entry, exit_px, detail = _premium_from_bars(bars, detect_ts)
        if entry is None:
            details.append(f"{opt}{strike}:no_bars")
            continue
        out[key_p] = entry
        out[key_e] = exit_px
        details.append(f"{inst['tradingsymbol']}:{detail}")

    if out["ce_premium"] is None:
        out["ce_premium"] = cas_entry_premium(
            close_px, ce_strike, "CE", strike_gap, iv, entry_minutes, 0.0
        )
        out["ce_exit"] = intrinsic(close_px, ce_strike, "CE")
        details.append("CE:bs_fallback")
    if out["pe_premium"] is None:
        out["pe_premium"] = cas_entry_premium(
            close_px, pe_strike, "PE", strike_gap, iv, entry_minutes, 0.0
        )
        out["pe_exit"] = intrinsic(close_px, pe_strike, "PE")
        details.append("PE:bs_fallback")

    if any("open=" in d for d in details):
        out["premium_source"] = "kite_option_minute"
    out["detail"] = " | ".join(details)
    # P&L from short premium
    out["pnl"] = (
        (float(out["ce_premium"]) - float(out["ce_exit"]))
        + (float(out["pe_premium"]) - float(out["pe_exit"]))
    ) * qty
    return out


def _synthetic_day_candles(
    index: str, d: date, seed: int = 0, anchor_close: Optional[float] = None
) -> List[dict]:
    import random

    rng = random.Random(seed + d.toordinal() + hash(index) % 997)
    # Realistic anchors (Aug-2026 ballpark). Prefer explicit anchor_close when known.
    level = anchor_close or (24500.0 if index == "NIFTY" else 79000.0)
    # Build an hour of noise ending near ``level``
    px = level * (1 + rng.uniform(-0.004, 0.004))
    candles = []
    for m in range(60):
        o = px
        # Pull toward target close as we approach the end
        target = level
        pull = (target - o) * (0.02 + 0.06 * (m / 59.0))
        move = pull + o * rng.uniform(-0.0008, 0.0008)
        c = o + move
        h = max(o, c) * (1 + abs(rng.uniform(0, 0.0003)))
        low = min(o, c) * (1 - abs(rng.uniform(0, 0.0003)))
        ts = datetime(d.year, d.month, d.day, 14, 30, tzinfo=IST) + timedelta(minutes=m)
        candles.append({"date": ts, "open": o, "high": h, "low": low, "close": c, "volume": 0})
        px = c
    # Force last close to anchor so ATM matches the real CAS close when provided
    if candles and anchor_close:
        candles[-1]["close"] = float(anchor_close)
        candles[-1]["high"] = max(candles[-1]["high"], float(anchor_close))
        candles[-1]["low"] = min(candles[-1]["low"], float(anchor_close))
    return candles


def _fetch_day_close(kite: Any, index: str, d: date) -> Optional[float]:
    """Official/day close from Kite daily candle (best CAS proxy available)."""
    meta = INDEX_META[index]
    try:
        from_dt = datetime.combine(d, datetime.min.time())
        to_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=23)
        rows = kite.historical_data(int(meta["token"]), from_dt, to_dt, "day")
        if not rows:
            return None
        return float(rows[-1]["close"])
    except Exception as exc:
        logger.warning("daily close fetch failed %s %s: %s", index, d, exc)
        return None


def _fetch_minute_candles(kite: Any, index: str, d: date) -> List[dict]:
    meta = INDEX_META[index]
    try:
        from_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=14, minutes=30)
        to_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=15, minutes=35)
        return list(kite.historical_data(int(meta["token"]), from_dt, to_dt, "minute") or [])
    except Exception as exc:
        logger.warning("minute fetch failed %s %s: %s", index, d, exc)
        return []


def _cas_window_stamp(d: date, rng_seed: int = 0) -> datetime:
    """Random CAS print time between 15:28 and 15:30 IST (matches OE Session II)."""
    import random

    rng = random.Random(rng_seed + d.toordinal())
    # seconds offset within [15:28:00, 15:30:00)
    offset = rng.randint(0, 119)
    return datetime(d.year, d.month, d.day, 15, 28, 0, tzinfo=IST) + timedelta(seconds=offset)


class _TimedFireProbe:
    """Detects CAS close on the tick bus and records wall-clock detect time."""

    def __init__(self, index: str, token: int, baseline: float, session_date: date):
        self.index = index
        self.token = token
        self.baseline = baseline
        self.session_date = session_date
        self.fired_close: Optional[float] = None
        self.trigger: Optional[str] = None
        self.ticks = 0
        self.cas_detected_at: Optional[str] = None
        self._detect_perf: Optional[float] = None

    def on_ticks(self, ticks: List[dict]) -> None:
        for tick in ticks:
            self.ticks += 1
            if self.fired_close is not None:
                return
            if int(tick.get("instrument_token") or 0) != self.token:
                continue
            ltp = float(tick.get("last_price") or 0)

            # Backtest fires only on the explicit CAS close marker tick so
            # timestamps land in the 15:28–15:30 window (not earlier candles).
            if not tick.get("cas_close") or not ltp:
                continue

            self.fired_close = float((tick.get("ohlc") or {}).get("close") or ltp)
            self.trigger = "ws_replay_cas"
            ts = tick.get("cas_ts") or tick.get("timestamp")
            if isinstance(ts, datetime):
                self.cas_detected_at = ts.astimezone(IST).isoformat(timespec="milliseconds")
            else:
                self.cas_detected_at = _cas_window_stamp(
                    self.session_date, hash(self.index) % 1000
                ).isoformat(timespec="milliseconds")
            self._detect_perf = time.perf_counter()
            return


def _simulate_sells(
    detect_iso: str,
    detect_perf: float,
    index: str,
    close_px: float,
    ce_strike: int,
    pe_strike: int,
    trigger: str,
) -> TimingEvent:
    """Stamp CE/PE sell times relative to detect (measures local path latency)."""
    timing = new_detect_event(
        index, close_px, trigger, source="backtest", detected_at=detect_iso
    )
    base = datetime.fromisoformat(detect_iso)

    ce_at = time.perf_counter()
    ce_ms = max((ce_at - detect_perf) * 1000.0, 0.05)
    timing.detect_to_ce_ms = round(ce_ms, 3)
    timing.ce_sold_at = (base + timedelta(milliseconds=ce_ms)).isoformat(timespec="milliseconds")
    timing.ce_symbol = f"{index}{ce_strike}CE"

    pe_at = time.perf_counter()
    pe_ms = max((pe_at - detect_perf) * 1000.0, ce_ms + 0.05)
    timing.detect_to_pe_ms = round(pe_ms, 3)
    timing.pe_sold_at = (base + timedelta(milliseconds=pe_ms)).isoformat(timespec="milliseconds")
    timing.pe_symbol = f"{index}{pe_strike}PE"
    timing.detect_to_done_ms = timing.detect_to_pe_ms
    timing.dry_run = True
    timing.extra = {"path": "ws_backtest_replay"}
    return timing


def run_ws_backtest(
    kite: Optional[Any] = None,
    config: Optional[AppConfig] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    capital: Optional[float] = None,
    close_overrides: Optional[Dict[str, float]] = None,
) -> BacktestResult:
    """Run expiry-day WebSocket replay backtest.

    Args:
        close_overrides: Optional map of real CAS closes to force when Kite
            historical is missing. Keys accepted:
              - ``YYYY-MM-DD`` (applies to that day's index)
              - ``YYYY-MM-DD:SENSEX`` / ``YYYY-MM-DD:NIFTY``
              - bare index name ``SENSEX`` / ``NIFTY`` (applies every day)
    """
    cfg = config or load_config()
    end = end or date.today()
    start = start or (end - timedelta(days=90))
    capital = float(capital or cfg.default_capital)
    overrides = {str(k).upper(): float(v) for k, v in (close_overrides or {}).items()}
    notes = [
        "Ticks replayed through the same WebSocket TickBus contract as live trading.",
        "cas_detected_at is stamped in the 15:28–15:30 IST CAS window.",
        "ce_sold_at / pe_sold_at measure detect→sell path latency on the replay bus.",
        f"OTM steps CE=+{cfg.ce_otm_steps} PE=-{cfg.pe_otm_steps}, lots={cfg.lots}.",
        (
            "Entry premium: real Kite option minute candles at CAS detect when available; "
            "otherwise BS fallback (no fixed ₹100 floor)."
        ),
        "Close priority: manual override → Kite daily/minute → synthetic (last resort).",
    ]

    equity = capital
    peak = capital
    max_dd = 0.0
    trades: List[BacktestTrade] = []
    timings: List[Dict[str, object]] = []
    curve: List[Dict[str, object]] = [{"date": start.isoformat(), "equity": equity}]
    ws_ticks_total = 0
    done_ms_list: List[float] = []

    def _resolve_override(day: date, index: str) -> Optional[float]:
        keys = [
            f"{day.isoformat()}:{index}".upper(),
            day.isoformat().upper(),
            index.upper(),
        ]
        for key in keys:
            if key in overrides:
                return float(overrides[key])
        return None

    d = start
    while d <= end:
        for index in indexes_for_date(d, cfg):
            meta = INDEX_META[index]
            token = int(meta["token"])
            gap = int(meta["strike_gap"])
            lot = int(meta["default_lot"])
            qty = cfg.lots * lot

            manual_close = _resolve_override(d, index)
            data_source = "synthetic"
            day_close: Optional[float] = None
            candles: List[dict] = []

            if kite is not None:
                day_close = _fetch_day_close(kite, index, d)
                candles = _fetch_minute_candles(kite, index, d)
                if candles:
                    data_source = "kite_minute"
                elif day_close is not None:
                    data_source = "kite_daily"
                    candles = _synthetic_day_candles(index, d, anchor_close=day_close)

            # Manual override wins for the CAS equilibrium price
            cas_close_override = manual_close if manual_close is not None else day_close
            if manual_close is not None:
                data_source = (
                    f"{data_source}+manual_close"
                    if data_source != "synthetic"
                    else "manual_close"
                )
                candles = _synthetic_day_candles(index, d, anchor_close=manual_close)
                notes.append(
                    f"{d} {index}: using MANUAL close override {manual_close}"
                )

            if not candles:
                candles = _synthetic_day_candles(
                    index, d, anchor_close=cas_close_override
                )
                if cas_close_override is None:
                    data_source = "synthetic"
                    notes.append(
                        f"{d} {index}: SYNTHETIC close (no Kite + no override). "
                        "Paste access_token or set Force close."
                    )

            cas_ts = _cas_window_stamp(d, hash(index) % 1000)
            baseline = float(candles[0]["open"])
            ticks = candle_to_ticks(token, candles, ticks_per_candle=4)
            if ticks:
                if cas_close_override is not None:
                    ticks[-1]["last_price"] = float(cas_close_override)
                    ticks[-1]["ohlc"] = {
                        **(ticks[-1].get("ohlc") or {}),
                        "close": float(cas_close_override),
                    }
                ticks[-1]["cas_ts"] = cas_ts
                ticks[-1]["timestamp"] = cas_ts
                ticks[-1]["cas_close"] = True

            bus = TickBus()
            probe = _TimedFireProbe(index, token, baseline, d)
            bus.add_handler(probe.on_ticks)
            n = TickReplay(bus).run(ticks, interval_ms=0)
            ws_ticks_total += n

            if probe.fired_close is None:
                close_px = float(
                    cas_close_override
                    if cas_close_override is not None
                    else candles[-1]["close"]
                )
                trigger = "fallback_close"
                detect_iso = cas_ts.isoformat(timespec="milliseconds")
                detect_perf = time.perf_counter()
            else:
                close_px = (
                    float(cas_close_override)
                    if cas_close_override is not None
                    else float(probe.fired_close)
                )
                trigger = probe.trigger or "ws"
                detect_iso = probe.cas_detected_at or cas_ts.isoformat(
                    timespec="milliseconds"
                )
                detect_perf = probe._detect_perf or time.perf_counter()

            atm, ce_k, pe_k = otm_strikes(
                close_px, gap, cfg.ce_otm_steps, cfg.pe_otm_steps
            )
            timing = _simulate_sells(
                detect_iso, detect_perf, index, close_px, ce_k, pe_k, trigger
            )
            timings.append(timing.to_dict())
            if timing.detect_to_done_ms is not None:
                done_ms_list.append(float(timing.detect_to_done_ms))

            detect_dt = datetime.fromisoformat(detect_iso)
            if kite is not None:
                legs = _live_option_pnl_legs(
                    kite=kite,
                    index=index,
                    expiry=d,
                    close_px=close_px,
                    ce_strike=ce_k,
                    pe_strike=pe_k,
                    detect_ts=detect_dt,
                    qty=qty,
                    iv=cfg.assumed_iv,
                    entry_minutes=cfg.entry_time_minutes,
                    strike_gap=gap,
                )
                ce_p = float(legs["ce_premium"])
                pe_p = float(legs["pe_premium"])
                ce_s = float(legs["ce_exit"])
                pe_s = float(legs["pe_exit"])
                pnl = float(legs["pnl"])
                premium_source = str(legs["premium_source"])
                prem_detail = str(legs["detail"])
            else:
                ce_p = cas_entry_premium(
                    close_px, ce_k, "CE", gap, cfg.assumed_iv, cfg.entry_time_minutes, 0.0
                )
                pe_p = cas_entry_premium(
                    close_px, pe_k, "PE", gap, cfg.assumed_iv, cfg.entry_time_minutes, 0.0
                )
                ce_s = intrinsic(close_px, ce_k, "CE")
                pe_s = intrinsic(close_px, pe_k, "PE")
                pnl = ((ce_p - ce_s) + (pe_p - pe_s)) * qty
                premium_source = "bs_fallback"
                prem_detail = "no kite session"

            equity += pnl
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100 if peak else 0
            max_dd = max(max_dd, dd)

            trades.append(
                BacktestTrade(
                    entry_date=d.isoformat(),
                    index=index,
                    close_price=round(close_px, 2),
                    atm=atm,
                    ce_strike=ce_k,
                    pe_strike=pe_k,
                    ce_premium=round(ce_p, 2),
                    pe_premium=round(pe_p, 2),
                    ce_settlement=round(ce_s, 2),
                    pe_settlement=round(pe_s, 2),
                    lot_size=lot,
                    quantity=qty,
                    pnl=round(pnl, 2),
                    ticks_replayed=n,
                    trigger=trigger,
                    data_source=data_source,
                    premium_source=premium_source,
                    cas_detected_at=timing.cas_detected_at,
                    ce_sold_at=timing.ce_sold_at or "",
                    pe_sold_at=timing.pe_sold_at or "",
                    detect_to_ce_ms=float(timing.detect_to_ce_ms or 0),
                    detect_to_pe_ms=float(timing.detect_to_pe_ms or 0),
                    detect_to_done_ms=float(timing.detect_to_done_ms or 0),
                    note=(
                        f"SELL CE {ce_k} @₹{ce_p:.2f} → exit ₹{ce_s:.2f}; "
                        f"SELL PE {pe_k} @₹{pe_p:.2f} → exit ₹{pe_s:.2f}; "
                        f"qty={qty}; spot={data_source}; prem={premium_source}; {prem_detail}"
                    ),
                )
            )
            curve.append(
                {"date": d.isoformat(), "equity": round(equity, 2), "index": index}
            )

        d += timedelta(days=1)

    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    total_pnl = equity - capital
    avg_done = sum(done_ms_list) / len(done_ms_list) if done_ms_list else 0.0
    return BacktestResult(
        strategy="cas_rule_ws_otm_sell",
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        initial_capital=capital,
        final_capital=round(equity, 2),
        total_pnl=round(total_pnl, 2),
        total_return_pct=round(total_pnl / capital * 100, 4) if capital else 0,
        num_trades=len(trades),
        winning_trades=wins,
        losing_trades=losses,
        win_rate_pct=round(wins / len(trades) * 100, 2) if trades else 0,
        max_drawdown_pct=round(max_dd, 4),
        avg_detect_to_done_ms=round(avg_done, 3),
        equity_curve=curve,
        trades=[asdict(t) for t in trades],
        timings=timings,
        notes=notes,
        ws_ticks_total=ws_ticks_total,
    )
