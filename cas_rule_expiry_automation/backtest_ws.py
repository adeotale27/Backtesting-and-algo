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
    pnl: float
    ticks_replayed: int
    trigger: str
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


def _synthetic_day_candles(index: str, d: date, seed: int = 0) -> List[dict]:
    import random

    rng = random.Random(seed + d.toordinal() + hash(index) % 997)
    level = 24500.0 if index == "NIFTY" else 81000.0
    level *= 1 + rng.uniform(-0.02, 0.02)
    candles = []
    px = level
    for m in range(60):
        o = px
        move = rng.uniform(-0.0015, 0.0015)
        c = o * (1 + move)
        h = max(o, c) * (1 + abs(rng.uniform(0, 0.0004)))
        low = min(o, c) * (1 - abs(rng.uniform(0, 0.0004)))
        ts = datetime(d.year, d.month, d.day, 14, 30, tzinfo=IST) + timedelta(minutes=m)
        candles.append({"date": ts, "open": o, "high": h, "low": low, "close": c, "volume": 0})
        px = c
    return candles


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
) -> BacktestResult:
    cfg = config or load_config()
    end = end or date.today()
    start = start or (end - timedelta(days=90))
    capital = float(capital or cfg.default_capital)
    notes = [
        "Ticks replayed through the same WebSocket TickBus contract as live trading.",
        "cas_detected_at is stamped in the 15:28–15:30 IST CAS window.",
        "ce_sold_at / pe_sold_at measure detect→sell path latency on the replay bus.",
        f"OTM steps CE=+{cfg.ce_otm_steps} PE=-{cfg.pe_otm_steps}, lots={cfg.lots}.",
    ]

    equity = capital
    peak = capital
    max_dd = 0.0
    trades: List[BacktestTrade] = []
    timings: List[Dict[str, object]] = []
    curve: List[Dict[str, object]] = [{"date": start.isoformat(), "equity": equity}]
    ws_ticks_total = 0
    done_ms_list: List[float] = []

    d = start
    while d <= end:
        for index in indexes_for_date(d, cfg):
            meta = INDEX_META[index]
            token = int(meta["token"])
            gap = int(meta["strike_gap"])
            lot = int(meta["default_lot"])
            qty = cfg.lots * lot

            candles: List[dict] = []
            if kite is not None:
                try:
                    from_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=14, minutes=30)
                    to_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=15, minutes=30)
                    candles = kite.historical_data(token, from_dt, to_dt, "minute")
                except Exception as exc:
                    notes.append(f"{d} {index}: historical failed ({exc})")
                    candles = []
            if not candles:
                candles = _synthetic_day_candles(index, d)

            # Attach CAS print timestamp onto the final close tick
            cas_ts = _cas_window_stamp(d, hash(index) % 1000)
            baseline = float(candles[0]["open"])
            ticks = candle_to_ticks(token, candles, ticks_per_candle=4)
            if ticks:
                ticks[-1]["cas_ts"] = cas_ts
                ticks[-1]["timestamp"] = cas_ts

            bus = TickBus()
            probe = _TimedFireProbe(index, token, baseline, d)
            bus.add_handler(probe.on_ticks)
            n = TickReplay(bus).run(ticks, interval_ms=0)
            ws_ticks_total += n

            if probe.fired_close is None:
                close_px = float(candles[-1]["close"])
                trigger = "fallback_close"
                detect_iso = cas_ts.isoformat(timespec="milliseconds")
                detect_perf = time.perf_counter()
            else:
                close_px = probe.fired_close
                trigger = probe.trigger or "ws"
                detect_iso = probe.cas_detected_at or cas_ts.isoformat(timespec="milliseconds")
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

            t_days = 5.0 / (60 * 24)
            ce_p = bs_price(close_px, ce_k, cfg.assumed_iv, t_days, "CE")
            pe_p = bs_price(close_px, pe_k, cfg.assumed_iv, t_days, "PE")
            ce_s = intrinsic(close_px, ce_k, "CE")
            pe_s = intrinsic(close_px, pe_k, "PE")
            pnl = ((ce_p - ce_s) + (pe_p - pe_s)) * qty
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
                    pnl=round(pnl, 2),
                    ticks_replayed=n,
                    trigger=trigger,
                    cas_detected_at=timing.cas_detected_at,
                    ce_sold_at=timing.ce_sold_at or "",
                    pe_sold_at=timing.pe_sold_at or "",
                    detect_to_ce_ms=float(timing.detect_to_ce_ms or 0),
                    detect_to_pe_ms=float(timing.detect_to_pe_ms or 0),
                    detect_to_done_ms=float(timing.detect_to_done_ms or 0),
                    note=f"WS replay · {probe.ticks} ticks · CAS window stamp",
                )
            )
            curve.append({"date": d.isoformat(), "equity": round(equity, 2), "index": index})

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
