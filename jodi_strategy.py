"""Jodi Strategy v1.0 — Continuous Theta Harvesting Engine.

Backtesting engine that follows the Jodi Strategy spec exactly:

    Wake up → Create a Jodi → Harvest Theta → Book Profit
    → Create Another Jodi → Repeat Until Trading Stops

Key design points:
    * NOT an iron condor held to expiry — a continuous theta-selling engine
      where completed Jodis are immediately replaced by fresh ones.
    * Multiple concurrent Jodis (one at a time in this v1 by default, but
      the engine is written so max_concurrent > 1 works trivially).
    * Intraday 15-minute bars for precise SL, repair-delay and 12 PM /
      3:20 PM cutoffs. Falls back to daily bars with degraded intraday
      granularity if the Kite subscription lacks intraday history.
    * Option premiums are Black-Scholes synthesised from NIFTY spot +
      India VIX because historical option-chain data is not part of the
      standard Kite Historical plan.
    * All rule numbers and behaviour is derived from JODI STRATEGY v1.0
      (Continuous Theta Harvesting Engine).
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — every knob mentioned in the spec is exposed here.
# ---------------------------------------------------------------------------

@dataclass
class JodiConfig:
    # Instrument
    lot_size: int = 65                  # NIFTY as of 2026
    strike_gap: int = 50                # NIFTY strikes
    margin_per_lot: float = 200_000.0   # ₹2L per Jodi as per spec
    risk_free_rate: float = 6.5

    # Strike selection knobs (Section 6)
    fri_premium_lo: float = 25.0
    fri_premium_hi: float = 40.0
    mon_am_premium_target: float = 20.0
    late_min_distance: int = 250          # Mon PM & Tue
    late_min_distance_fallback: int = 200
    late_min_premium: float = 10.0

    # Repair rules (Section 9-11) + premium-matching (v1.1)
    repair_delay_min: int = 15            # minimum wait after SL
    max_repairs_per_jodi: int = 2
    repair_tolerance_abs: float = 2.0     # ±₹2 default
    repair_tolerance_pct: float = 10.0    # or ±10 % (whichever is larger)
    repair_min_premium: float = 5.0       # never repair with a cheaper strike
    min_combined_premium: float = 10.0    # skip jodi if new_sum below this
    stabilization_conditions_required: int = 2  # need >= this many of 5 checks

    # Profit booking (Section 12)
    profit_book_pct: float = 0.20          # book surviving short at <= 20% of entry

    # Daily risk (Section 14-15)
    daily_loss_limit_pct: float = 1.0
    daily_soft_loss_pct: float = 0.6       # soft cap: stop new entries, keep managing
    daily_profit_target_pct: float = 2.0

    # NEW ── EOD carry-forward (after profit target exit) + VIX-spike gate
    carry_forward_start_time: time = time(15, 0)
    vix_spike_threshold_pct: float = 15.0     # VIX up >15% vs previous close = "extreme"
    hedge_distance_pct: float = 1.5           # hedge strike = short ± 1.5% of ATM

    # NEW ── Portfolio limits
    capital_per_jodi: float = 2_500_000.0     # 1 concurrent Jodi allowed per ₹25L
    min_strike_separation_pts: int = 100      # new Jodi strikes must be ≥ this from existing Jodis
    min_short_leg_premium: float = 10.0       # each short leg must sell ≥ ₹10 (v1.3)

    # Time-of-day rules (Sections 3, 16)
    entry_start_time: time = time(9, 30)      # first entry each day at 9:30 (Mon/Tue)
    friday_entry_time: time = time(15, 15)    # Friday: ENTER LATE to harvest Sat+Sun theta
    tuesday_no_new_after: time = time(12, 0)
    hard_exit_time: time = time(15, 20)

    # Engine
    max_concurrent_jodis: int = 3          # multiple concurrent Jodis
    slippage_pct: float = 2.0              # % slippage on SL exit
    dynamic_sizing: bool = True            # rescale lot count with current equity daily


# ---------------------------------------------------------------------------
# Leg / Jodi state
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    side: str            # "PE" / "CE"
    direction: str       # "short" / "long"
    strike: int
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    qty: int = 0
    is_open: bool = True
    exit_reason: str = ""

    @property
    def realized_pnl(self) -> float:
        if self.exit_price is None or self.is_open:
            return 0.0
        # short: profit when exit < entry;  long: profit when exit > entry
        sign = 1 if self.direction == "short" else -1
        return sign * (self.entry_price - self.exit_price) * self.qty


@dataclass
class Jodi:
    id: int
    entry_time: datetime
    expiry: date
    short_pe: Leg
    short_ce: Leg
    long_pe: Leg
    long_ce: Leg
    sum_short: float
    per_leg_sl: float
    repairs_used: int = 0
    repair_pending_side: Optional[str] = None    # "PE" or "CE" awaiting repair
    repair_ready_at: Optional[datetime] = None
    status: str = "active"       # active | one_side_dead | profit_booked | expired | abandoned
    booked_pnl: float = 0.0
    profit_target: float = 0.0
    theta_collected: float = 0.0

    def all_legs(self) -> List[Leg]:
        return [self.short_pe, self.short_ce, self.long_pe, self.long_ce]

    def any_short_open(self) -> bool:
        return self.short_pe.is_open or self.short_ce.is_open

    def surviving_short(self) -> Optional[Leg]:
        if self.short_pe.is_open and not self.short_ce.is_open:
            return self.short_pe
        if self.short_ce.is_open and not self.short_pe.is_open:
            return self.short_ce
        return None


# ---------------------------------------------------------------------------
# Black-Scholes helper (mibian)
# ---------------------------------------------------------------------------

def _bs_price(spot: float, strike: float, iv_pct: float, dte_days: float,
              opt_type: str, r_pct: float) -> float:
    try:
        import mibian
    except ImportError:
        return 0.0
    dte = max(dte_days, 0.5)
    bs = mibian.BS([spot, strike, r_pct, dte], volatility=max(iv_pct, 1.0))
    px = bs.callPrice if opt_type == "CE" else bs.putPrice
    return max(0.05, float(px))


def _bs_theta(spot: float, strike: float, iv_pct: float, dte_days: float,
              opt_type: str, r_pct: float) -> float:
    try:
        import mibian
    except ImportError:
        return 0.0
    dte = max(dte_days, 0.5)
    bs = mibian.BS([spot, strike, r_pct, dte], volatility=max(iv_pct, 1.0))
    return float(bs.callTheta if opt_type == "CE" else bs.putTheta)


# ---------------------------------------------------------------------------
# SL rounding rule (Section 7)
# ---------------------------------------------------------------------------

def jodi_sl_from_sum(sum_premium: float) -> float:
    """Sum ends in 1-5 → tens*10 + 5;  sum ends in 6-9 or 0 → (tens+1)*10 + 1."""
    s = int(round(sum_premium))
    last = s % 10
    base = s - last
    if last in (1, 2, 3, 4, 5):
        return float(base + 5)
    return float(base + 11)


# ---------------------------------------------------------------------------
# Strike selection helpers
# ---------------------------------------------------------------------------

def _round_strike(x: float, gap: int) -> int:
    return int(round(x / gap) * gap)


def _find_short_strike(
    spot: float, iv: float, dte: float, r: float,
    side: str, cfg: JodiConfig, bar_time: datetime, is_expiry_day: bool,
) -> Optional[Tuple[int, float]]:
    """Pick a short strike per Section 6 rules for the given side."""
    weekday = bar_time.weekday()   # 0=Mon .. 4=Fri
    hour = bar_time.time()
    center = _round_strike(spot, cfg.strike_gap)

    # Direction: PE strikes are BELOW spot; CE strikes are ABOVE.
    sign = -1 if side == "PE" else +1

    def _px(k: int) -> float:
        return _bs_price(spot, k, iv, dte, side, r)

    # Case A: Friday → target ₹25-40 premium
    if weekday == 4:
        # walk from ~10 strikes OTM outward and find first strike inside range
        best = None
        for step in range(6, 25):  # 300-1250 pts OTM roughly
            k = center + sign * step * cfg.strike_gap
            if k <= 0: continue
            p = _px(k)
            if cfg.fri_premium_lo <= p <= cfg.fri_premium_hi:
                # accept the first strike hitting the range
                return k, p
            if best is None or abs(p - (cfg.fri_premium_lo + cfg.fri_premium_hi) / 2) < abs(best[1] - (cfg.fri_premium_lo + cfg.fri_premium_hi) / 2):
                best = (k, p)
        return best

    # Case B: Monday BEFORE 12 PM → target ~₹20 (distance flexible)
    if weekday == 0 and hour < cfg.tuesday_no_new_after:
        best = None
        best_diff = 1e9
        for step in range(4, 25):
            k = center + sign * step * cfg.strike_gap
            if k <= 0: continue
            p = _px(k)
            if p < 1.0: continue
            diff = abs(p - cfg.mon_am_premium_target)
            if diff < best_diff:
                best_diff = diff
                best = (k, p)
        return best

    # Case C: Monday PM or Tuesday → min distance 250 (fallback 200), min premium ₹10
    for min_dist in (cfg.late_min_distance, cfg.late_min_distance_fallback):
        if min_dist < 200:
            break
        min_step = max(4, min_dist // cfg.strike_gap)
        best = None
        for step in range(min_step, min_step + 20):
            k = center + sign * step * cfg.strike_gap
            if k <= 0: continue
            p = _px(k)
            if p >= cfg.late_min_premium:
                return k, p
            if best is None:
                best = (k, p)
        if best and best[1] >= cfg.late_min_premium * 0.7:
            return best
    return None


def _find_repair_strike(
    spot: float, iv: float, dte: float, r: float,
    side: str, target_premium: float, cfg: JodiConfig,
) -> Optional[Tuple[int, float]]:
    """Find OTM strike whose BS price matches target_premium within tolerance.

    Rules (Jodi v1.1 premium-pairing):
        • tolerance = max(±₹repair_tolerance_abs, ±repair_tolerance_pct % of target)
        • must satisfy min_distance rule (fallback = late_min_distance_fallback)
        • strike premium ≥ repair_min_premium (never repair with ₹4-₹5 far-OTM)
        • never ATM
    Returns (strike, price) if a match exists; None otherwise.
    """
    tol = max(cfg.repair_tolerance_abs, target_premium * cfg.repair_tolerance_pct / 100.0)
    lo = max(cfg.repair_min_premium, target_premium - tol)
    hi = target_premium + tol
    center = _round_strike(spot, cfg.strike_gap)
    sign = -1 if side == "PE" else +1
    min_step = max(4, cfg.late_min_distance_fallback // cfg.strike_gap)

    best: Optional[Tuple[int, float]] = None
    best_diff = float("inf")
    for step in range(min_step, min_step + 40):
        k = center + sign * step * cfg.strike_gap
        if k <= 0 or abs(k - spot) < 100:   # never ATM
            continue
        p = _bs_price(spot, k, iv, dte, side, r)
        if p < cfg.repair_min_premium:
            continue
        if lo <= p <= hi:
            diff = abs(p - target_premium)
            if diff < best_diff:
                best_diff = diff
                best = (k, p)
    return best   # None ⇒ no quality match → skip repair (Jodi v1.1 rule)


# ---------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------

def _tuesday_expiries(start: date, end: date, holidays: set) -> List[date]:
    out = []
    d = start
    while d <= end + timedelta(days=14):
        if d.weekday() == 1:  # Tue
            exp = d
            if exp in holidays:
                exp -= timedelta(days=1)
            out.append(exp)
        d += timedelta(days=1)
    return out


def _load_indian_holidays(y0: int, y1: int) -> set:
    try:
        import holidays as _hol
        s = set()
        for y in range(y0, y1 + 1):
            for d, _n in _hol.India(years=y).items():
                s.add(d)
        return s
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

@dataclass
class EngineOutput:
    trades: List[Dict[str, Any]] = field(default_factory=list)
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    jodis: List[Dict[str, Any]] = field(default_factory=list)


class JodiEngine:
    def __init__(self, cfg: JodiConfig, capital: float, kite: Any):
        self.cfg = cfg
        self.initial_capital = capital
        self.equity = capital
        self.kite = kite
        self.max_lots = max(1, int(capital // cfg.margin_per_lot))
        self.qty_per_leg = self.max_lots * cfg.lot_size
        # Initial capital-scaled max concurrent Jodis (Rule 4)
        cfg.max_concurrent_jodis = max(1, int(capital // cfg.capital_per_jodi))
        self.jodis: List[Jodi] = []
        self.next_jodi_id = 1
        self.output = EngineOutput()

        # Daily state
        self.trading_disabled_until: Optional[date] = None
        self.day_start_equity = capital
        self.day_realized = 0.0
        self.last_bar_date: Optional[date] = None
        # NEW: profit-target + VIX spike + hard-loss pause state
        self.profit_target_hit_today: bool = False
        self.hard_loss_hit_today: bool = False
        self.vix_spiked_today: bool = False
        self.prev_day_vix: Optional[float] = None
        self.today_vix: Optional[float] = None
        # Rolling bar history for ATR / ADX / range checks (last 30 bars)
        self._recent_bars: List[Dict] = []

    # ---------------------------------------------------------------------
    # Public entry point
    # ---------------------------------------------------------------------

    def run(self, start: date, end: date) -> EngineOutput:
        from kiteconnect import KiteConnect  # noqa: F401 for type hint sanity

        # Fetch NIFTY 15-min candles + VIX daily.
        spot_bars, vix_by_date, granularity = self._fetch_data(start, end)
        if not spot_bars:
            raise ValueError("No NIFTY candle data available for the range.")
        self.output.events.append(f"Data granularity: {granularity}. Bars: {len(spot_bars)}.")

        holidays = _load_indian_holidays(start.year, end.year)
        tuesdays = _tuesday_expiries(start, end, holidays)
        expiries_map = {}
        for d in _daterange(start, end):
            expiries_map[d] = _next_expiry(d, tuesdays)

        self.output.events.append(
            f"Capital ₹{self.initial_capital:,.0f} | {self.max_lots} lot(s)/Jodi | qty/leg={self.qty_per_leg}"
        )

        for bar in spot_bars:
            self._process_bar(bar, vix_by_date, expiries_map)

        # Force-close any leftover position at end of range.
        if spot_bars:
            self._force_close_all(spot_bars[-1], vix_by_date, expiries_map, reason="range_end")

        # Finalize equity curve  ─  ensure we have at least one point per day.
        return self.output

    # ---------------------------------------------------------------------
    # Bar processing
    # ---------------------------------------------------------------------

    def _process_bar(self, bar: Dict, vix_by_date: Dict, expiries_map: Dict):
        bar_time: datetime = bar["date"]
        bar_date: date = bar_time.date()
        weekday = bar_time.weekday()

        # ── Keep rolling recent bars for ATR / ADX / range checks ──
        self._recent_bars.append(bar)
        if len(self._recent_bars) > 30:
            self._recent_bars = self._recent_bars[-30:]

        # ── Roll daily state ──
        if self.last_bar_date != bar_date:
            self.day_start_equity = self.equity
            self.day_realized = 0.0
            self.last_bar_date = bar_date
            self.profit_target_hit_today = False
            self.hard_loss_hit_today = False
            # Dynamic capital sizing: recompute lots per Jodi based on current
            # equity so position size grows with profits and shrinks after losses.
            if self.cfg.dynamic_sizing:
                new_lots = max(1, int(self.equity // self.cfg.margin_per_lot))
                if new_lots != self.max_lots:
                    self.output.events.append(
                        f"[{bar_time:%Y-%m-%d %H:%M}] 📊 Sizing rescaled: {self.max_lots} → {new_lots} lots "
                        f"(equity ₹{self.equity:,.0f})"
                    )
                    self.max_lots = new_lots
                    self.qty_per_leg = new_lots * self.cfg.lot_size

                # Also rescale max concurrent Jodis with capital (Rule 4).
                new_max_concurrent = max(1, int(self.equity // self.cfg.capital_per_jodi))
                if new_max_concurrent != self.cfg.max_concurrent_jodis:
                    self.output.events.append(
                        f"[{bar_time:%Y-%m-%d %H:%M}] 🔢 Max concurrent Jodis: "
                        f"{self.cfg.max_concurrent_jodis} → {new_max_concurrent} "
                        f"(₹{self.cfg.capital_per_jodi:,.0f}/Jodi)"
                    )
                    self.cfg.max_concurrent_jodis = new_max_concurrent
            # VIX spike check — new day's close will only be known at EOD but
            # kite gives us the closing VIX per date. Use previous day's close
            # vs today's expected close as a proxy right at open.
            self.prev_day_vix = self.today_vix
            self.today_vix = vix_by_date.get(bar_date)
            if self.prev_day_vix and self.today_vix:
                change = (self.today_vix - self.prev_day_vix) / self.prev_day_vix * 100
                self.vix_spiked_today = change >= self.cfg.vix_spike_threshold_pct
                if self.vix_spiked_today:
                    self.output.events.append(
                        f"[{bar_time:%Y-%m-%d %H:%M}] ⚠ VIX SPIKE {self.prev_day_vix:.2f}→{self.today_vix:.2f} (+{change:.1f}%) "
                        f"→ carry-forward disabled for today"
                    )
            else:
                self.vix_spiked_today = False

        iv = vix_by_date.get(bar_date, 15.0)
        expiry = expiries_map.get(bar_date)
        r = self.cfg.risk_free_rate

        # ── Compute spot representative prices ──
        spot_open = bar.get("open") or bar.get("close")
        spot_close = bar["close"]
        spot_high = bar.get("high", spot_close)
        spot_low = bar.get("low", spot_close)

        # 1. Force-exit at 3:20 PM on any trading day (Section 16).
        if bar_time.time() >= self.cfg.hard_exit_time:
            self._force_close_all(bar, vix_by_date, expiries_map, reason="hard_exit_320")
            self._record_equity(bar_time)
            return

        # 2. Manage existing Jodis (SL check per leg, profit target, expiry).
        self._manage_jodis(bar, spot_high, spot_low, spot_close, iv, r)

        # 3. Attempt pending repairs (10-15 min after SL).
        self._process_repairs(bar, spot_close, iv, r, expiry)

        # 4. Daily HARD loss circuit-breaker (1 % of day-start equity).
        # Behaviour (v1.4): DO NOT force-close positions. Existing profitable
        # legs continue to run with their per-leg SL/target. Only NEW entries
        # are paused until the 15:15 carry-forward re-entry window opens.
        if not self.hard_loss_hit_today and self._daily_loss_hit():
            self.hard_loss_hit_today = True
            realised_loss = self.day_realized
            self.output.events.append(
                f"[{bar_time:%Y-%m-%d %H:%M}] ⛔ HARD LOSS 1% HIT (realised ₹{realised_loss:,.0f}). "
                f"Keeping open legs at their SL/target. New entries paused until 15:15."
            )

        # 4b. Daily PROFIT target: if realised P&L for the day >= 2 % of
        # day-start equity, exit every open position immediately. New Jodis
        # may still be opened later in the day for a carry-forward trade
        # (only on Fri / Mon, and only if VIX has not spiked today).
        if not self.profit_target_hit_today:
            pnl_pct = self.day_realized / max(1.0, self.day_start_equity) * 100
            if pnl_pct >= self.cfg.daily_profit_target_pct:
                self._force_close_all(bar, vix_by_date, expiries_map, reason="profit_target_hit")
                self.profit_target_hit_today = True
                self.output.events.append(
                    f"[{bar_time:%Y-%m-%d %H:%M}] ✅ DAILY PROFIT TARGET HIT ({pnl_pct:.2f}%) → all positions closed. "
                    f"Carry-forward window opens at {self.cfg.carry_forward_start_time:%H:%M}."
                )

        # 5. Attempt to create a new Jodi if room / rules allow.
        if self._is_valid_entry_bar(bar_time):
            self._try_create_jodi(bar, spot_close, iv, r, expiry)

        # 6. Equity mark-to-market.
        self._record_equity(bar_time, mtm_spot=spot_close, iv=iv, r=r, expiry=expiry)

    # ---------------------------------------------------------------------
    # Market-stabilization check (Jodi v1.1)
    # ---------------------------------------------------------------------

    def _is_market_stable(self, iv_now: float) -> Tuple[bool, List[str]]:
        """Return (stable, reasons_passed). Need >= stabilization_conditions_required."""
        bars = self._recent_bars
        if len(bars) < 5:
            return False, ["not enough bar history"]
        passed: List[str] = []
        curr = bars[-1]

        # 1) Price within previous bar's range (proxy for last 10-min range on 15-min data)
        if len(bars) >= 2:
            prev = bars[-2]
            if prev["low"] <= curr["close"] <= prev["high"]:
                passed.append("price_in_prev_range")

        # 2) Last 3 candles overlap
        if len(bars) >= 3:
            c1, c2, c3 = bars[-3], bars[-2], bars[-1]
            low_max = max(c1["low"], c2["low"], c3["low"])
            high_min = min(c1["high"], c2["high"], c3["high"])
            if low_max <= high_min:
                passed.append("3-candle overlap")

        # 3) ATR(14) decreasing
        atr_now  = self._atr(bars[-14:]) if len(bars) >= 14 else None
        atr_prev = self._atr(bars[-15:-1]) if len(bars) >= 15 else None
        if atr_now is not None and atr_prev is not None and atr_now < atr_prev:
            passed.append(f"ATR↓ {atr_now:.0f}<{atr_prev:.0f}")

        # 4) ADX(14) < 25 (weak trend / ranging)
        adx = self._adx(bars[-15:]) if len(bars) >= 15 else None
        if adx is not None and adx < 25:
            passed.append(f"ADX={adx:.1f}<25")

        # 5) IV not increasing today vs yesterday (allow 2 % noise)
        if self.prev_day_vix and iv_now:
            if iv_now <= self.prev_day_vix * 1.02:
                passed.append(f"IV≤prev {iv_now:.1f}≤{self.prev_day_vix:.1f}")

        return len(passed) >= self.cfg.stabilization_conditions_required, passed

    @staticmethod
    def _atr(bars: List[Dict]) -> Optional[float]:
        if len(bars) < 2: return None
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["high"], bars[i]["low"], bars[i-1]["close"]
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        return sum(trs) / len(trs) if trs else None

    @staticmethod
    def _adx(bars: List[Dict]) -> Optional[float]:
        """Simplified Wilder ADX(14). Returns None if insufficient data."""
        if len(bars) < 15: return None
        pdms, ndms, trs = [], [], []
        for i in range(1, len(bars)):
            up_move   = bars[i]["high"] - bars[i-1]["high"]
            down_move = bars[i-1]["low"] - bars[i]["low"]
            pdms.append(up_move   if up_move   > down_move and up_move   > 0 else 0)
            ndms.append(down_move if down_move > up_move   and down_move > 0 else 0)
            tr = max(
                bars[i]["high"] - bars[i]["low"],
                abs(bars[i]["high"] - bars[i-1]["close"]),
                abs(bars[i]["low"]  - bars[i-1]["close"]),
            )
            trs.append(tr)
        atr = sum(trs) / len(trs)
        if atr == 0: return None
        pdi = 100 * (sum(pdms) / len(pdms)) / atr
        ndi = 100 * (sum(ndms) / len(ndms)) / atr
        if pdi + ndi == 0: return None
        dx = 100 * abs(pdi - ndi) / (pdi + ndi)
        return dx   # single-period approximation

    def _is_valid_entry_bar(self, bar_time: datetime) -> bool:
        weekday = bar_time.weekday()
        # Fri (4), Mon (0), Tue (1) only. Tue: only before 12 PM.
        if weekday not in (0, 1, 4):
            return False
        if weekday == 1 and bar_time.time() >= self.cfg.tuesday_no_new_after:
            return False
        # ── Friday: enter LATE only (~15:15) to harvest Sat + Sun theta ──
        if weekday == 4:
            if bar_time.time() < self.cfg.friday_entry_time:
                return False
            # Don't enter after hard_exit_time on Friday either.
            if bar_time.time() >= self.cfg.hard_exit_time:
                return False
        else:
            if bar_time.time() < self.cfg.entry_start_time:
                return False
        # If daily loss limit hit and we're still in the same day, disallow.
        if self.trading_disabled_until == bar_time.date():
            return False
        # Rule (v1.4): HARD 1 % loss → pause new entries until 15:15 IST.
        # Existing positions keep running; a fresh Jodi may be created after
        # 15:15 to harvest overnight theta (subject to normal rules + VIX gate).
        if self.hard_loss_hit_today and bar_time.time() < self.cfg.friday_entry_time:
            return False
        # Rule 6 (v1.3): Soft loss cap at 0.6% — stop NEW entries, keep managing.
        # If losses recover (day_realized > -soft_cap) allow entries again.
        day_loss_pct = -(self.day_realized) / max(1.0, self.day_start_equity) * 100
        if day_loss_pct >= self.cfg.daily_soft_loss_pct:
            return False
        # ── After daily profit target hit ──
        # Only allow re-entry as CARRY-FORWARD trade:
        #   • Fri or Mon only (Tue never carries — expiry is that day)
        #   • Only after carry_forward_start_time (e.g. 15:00)
        #   • Only if VIX has NOT spiked today
        if self.profit_target_hit_today:
            if weekday not in (0, 4):
                return False
            if bar_time.time() < self.cfg.carry_forward_start_time:
                return False
            if self.vix_spiked_today:
                return False
            # Also make sure we don't infinite-loop — allow at most one carry-
            # forward wave by capping concurrent to max_concurrent_jodis (same
            # limit) after profit target.
        # Concurrency cap.
        active = sum(1 for j in self.jodis if j.status == "active")
        return active < self.cfg.max_concurrent_jodis

    def _try_create_jodi(self, bar, spot: float, iv: float, r: float, expiry: Optional[date]):
        if expiry is None:
            return
        dte = (datetime.combine(expiry, time(15, 30)) - bar["date"]).total_seconds() / 86400
        if dte <= 0:
            return
        is_expiry_day = (bar["date"].date() == expiry)

        pe_pick = _find_short_strike(spot, iv, dte, r, "PE", self.cfg, bar["date"], is_expiry_day)
        ce_pick = _find_short_strike(spot, iv, dte, r, "CE", self.cfg, bar["date"], is_expiry_day)
        if not pe_pick or not ce_pick:
            return

        pe_strike, pe_px = pe_pick
        ce_strike, ce_px = ce_pick

        # Rule 1: never ATM (belt-and-braces check).
        if abs(pe_strike - spot) < 100 or abs(ce_strike - spot) < 100:
            return

        # Rule 8 (v1.3): every short leg's premium must be ≥ min_short_leg_premium.
        if pe_px < self.cfg.min_short_leg_premium or ce_px < self.cfg.min_short_leg_premium:
            return

        # Rule 5 (v1.3): strike separation — new Jodi's short strikes must be
        # at least min_strike_separation_pts away from every active Jodi's
        # short strikes on the same side.
        for existing in self.jodis:
            if existing.status != "active":
                continue
            if existing.short_pe.is_open and abs(existing.short_pe.strike - pe_strike) < self.cfg.min_strike_separation_pts:
                return
            if existing.short_ce.is_open and abs(existing.short_ce.strike - ce_strike) < self.cfg.min_strike_separation_pts:
                return

        # Hedges — 1.5 % of ATM further OTM (SEBI/exchange-friendly hedge
        # distance so the long premium has meaningful value and actually
        # reduces span margin, instead of a nearly-worthless far OTM).
        hedge_pts = int(round(spot * self.cfg.hedge_distance_pct / 100.0 / self.cfg.strike_gap) * self.cfg.strike_gap)
        hedge_pts = max(hedge_pts, self.cfg.strike_gap * 2)   # at least 2 strikes away
        long_pe_strike = pe_strike - hedge_pts
        long_ce_strike = ce_strike + hedge_pts
        long_pe_px = _bs_price(spot, long_pe_strike, iv, dte, "PE", r)
        long_ce_px = _bs_price(spot, long_ce_strike, iv, dte, "CE", r)

        # Rule: minimum premium sanity — no zero-premium short.
        if pe_px < 3 or ce_px < 3:
            return

        sum_short = pe_px + ce_px
        # Jodi v1.1: minimum combined premium ₹10 — skip if below.
        if sum_short < self.cfg.min_combined_premium:
            return
        sl = jodi_sl_from_sum(sum_short)

        jid = self.next_jodi_id
        self.next_jodi_id += 1
        qty = self.qty_per_leg
        jodi = Jodi(
            id=jid, entry_time=bar["date"], expiry=expiry,
            short_pe=Leg("PE", "short", pe_strike, bar["date"], pe_px, qty=qty),
            short_ce=Leg("CE", "short", ce_strike, bar["date"], ce_px, qty=qty),
            long_pe =Leg("PE", "long",  long_pe_strike, bar["date"], long_pe_px, qty=qty),
            long_ce =Leg("CE", "long",  long_ce_strike, bar["date"], long_ce_px, qty=qty),
            sum_short=sum_short, per_leg_sl=sl,
            profit_target=self.cfg.profit_book_pct * sum_short,
        )
        self.jodis.append(jodi)
        self.output.events.append(
            f"[{bar['date']:%Y-%m-%d %H:%M}] JODI #{jid} CREATE  spot={spot:.0f} IV={iv:.1f}% DTE={dte:.2f}d  "
            f"SP {pe_strike}@{pe_px:.1f} + SC {ce_strike}@{ce_px:.1f}  "
            f"LP {long_pe_strike}@{long_pe_px:.1f} + LC {long_ce_strike}@{long_ce_px:.1f}  "
            f"sum={sum_short:.1f} → SL/leg=₹{sl:.0f}"
        )

    # ---------------------------------------------------------------------
    # SL / repair / expiry / booking
    # ---------------------------------------------------------------------

    def _manage_jodis(self, bar, spot_high, spot_low, spot_close, iv, r):
        bar_time = bar["date"]
        for jodi in self.jodis:
            if jodi.status not in ("active", "one_side_dead"):
                continue

            # Expiry settlement.
            if bar_time.date() >= jodi.expiry and bar_time.time() >= self.cfg.hard_exit_time:
                self._settle_expiry(jodi, spot_close, iv, r, bar_time)
                continue

            dte = max(
                (datetime.combine(jodi.expiry, time(15, 30)) - bar_time).total_seconds() / 86400,
                0.02,
            )

            # ── Per-leg SL check using bar high/low ──
            # PE peak = day low spot; CE peak = day high spot
            pe_hi_px = _bs_price(spot_low, jodi.short_pe.strike, iv, dte, "PE", r) if jodi.short_pe.is_open else 0.0
            ce_hi_px = _bs_price(spot_high, jodi.short_ce.strike, iv, dte, "CE", r) if jodi.short_ce.is_open else 0.0

            # PE side breach
            if jodi.short_pe.is_open and pe_hi_px >= jodi.per_leg_sl:
                exit_px = jodi.per_leg_sl * (1 + self.cfg.slippage_pct / 100)
                self._close_leg(jodi.short_pe, bar_time, exit_px, "SL_HIT")
                self.output.events.append(
                    f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} SL PE {jodi.short_pe.strike} "
                    f"exit ₹{exit_px:.1f} (SL=₹{jodi.per_leg_sl:.0f}) pnl ₹{jodi.short_pe.realized_pnl:,.0f}"
                )
                self._schedule_repair(jodi, "PE", bar_time)

            # CE side breach
            if jodi.short_ce.is_open and ce_hi_px >= jodi.per_leg_sl:
                exit_px = jodi.per_leg_sl * (1 + self.cfg.slippage_pct / 100)
                self._close_leg(jodi.short_ce, bar_time, exit_px, "SL_HIT")
                self.output.events.append(
                    f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} SL CE {jodi.short_ce.strike} "
                    f"exit ₹{exit_px:.1f} (SL=₹{jodi.per_leg_sl:.0f}) pnl ₹{jodi.short_ce.realized_pnl:,.0f}"
                )
                self._schedule_repair(jodi, "CE", bar_time)

            # ── Profit booking (surviving short decayed to <= 20% of entry, or full Jodi ≤ profit_target) ──
            surv = jodi.surviving_short()
            if surv and surv.is_open:
                surv_ltp = _bs_price(spot_close, surv.strike, iv, dte, surv.side, r)
                if surv_ltp <= max(0.5, self.cfg.profit_book_pct * surv.entry_price):
                    self._close_leg(surv, bar_time, surv_ltp, "PROFIT_BOOK")
                    self.output.events.append(
                        f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} PROFIT-BOOK surviving {surv.side} "
                        f"@ ₹{surv_ltp:.1f} (entry ₹{surv.entry_price:.1f})  pnl ₹{surv.realized_pnl:,.0f}"
                    )
                    self._maybe_close_hedges_and_complete(jodi, bar_time, spot_close, iv, dte, r, "profit_booked")
            elif jodi.short_pe.is_open and jodi.short_ce.is_open:
                pe_ltp = _bs_price(spot_close, jodi.short_pe.strike, iv, dte, "PE", r)
                ce_ltp = _bs_price(spot_close, jodi.short_ce.strike, iv, dte, "CE", r)
                if (pe_ltp + ce_ltp) <= jodi.profit_target:
                    # Book both shorts + close hedges.
                    self._close_leg(jodi.short_pe, bar_time, pe_ltp, "PROFIT_BOOK")
                    self._close_leg(jodi.short_ce, bar_time, ce_ltp, "PROFIT_BOOK")
                    self.output.events.append(
                        f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} PROFIT-BOOK both shorts "
                        f"pe ₹{pe_ltp:.1f} + ce ₹{ce_ltp:.1f} = {pe_ltp+ce_ltp:.1f} ≤ target ₹{jodi.profit_target:.1f}"
                    )
                    self._maybe_close_hedges_and_complete(jodi, bar_time, spot_close, iv, dte, r, "profit_booked")

    def _schedule_repair(self, jodi: Jodi, side: str, bar_time: datetime):
        # After 2 failed repairs, do NOT schedule a third — let surviving side run.
        if jodi.repairs_used >= self.cfg.max_repairs_per_jodi:
            jodi.status = "one_side_dead"
            self.output.events.append(
                f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} → max repairs used ({jodi.repairs_used}). "
                f"Letting surviving side run (Section 11)."
            )
            return
        jodi.repair_pending_side = side
        jodi.repair_ready_at = bar_time + timedelta(minutes=self.cfg.repair_delay_min)
        jodi.status = "active"  # still active
        self.output.events.append(
            f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} repair scheduled for {side} at "
            f"{jodi.repair_ready_at:%H:%M} (delay {self.cfg.repair_delay_min} min)"
        )

    def _process_repairs(self, bar, spot_close, iv, r, expiry):
        bar_time = bar["date"]
        for jodi in self.jodis:
            if jodi.status not in ("active",):
                continue
            if not jodi.repair_pending_side or bar_time < (jodi.repair_ready_at or bar_time):
                continue
            # Additional Jodi v1.1 gate: market must be stable (≥2 of 5 checks).
            stable, reasons = self._is_market_stable(iv)
            if not stable:
                # Push repair check to next bar — market not yet calm enough.
                continue
            # Do repair now.
            side = jodi.repair_pending_side
            surv = jodi.surviving_short()
            if surv is None or not surv.is_open:
                # Both dead — nothing to mirror.
                jodi.repair_pending_side = None
                jodi.repair_ready_at = None
                continue
            dte = max(
                (datetime.combine(jodi.expiry, time(15, 30)) - bar_time).total_seconds() / 86400,
                0.02,
            )
            target_px = _bs_price(spot_close, surv.strike, iv, dte, surv.side, r)

            # If surviving premium already too low → not worth repairing.
            if target_px < self.cfg.repair_min_premium:
                jodi.repair_pending_side = None
                jodi.repair_ready_at = None
                jodi.status = "one_side_dead"
                self.output.events.append(
                    f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} SKIP REPAIR — surviving {surv.side} ₹{target_px:.1f} "
                    f"< min ₹{self.cfg.repair_min_premium:.0f}. Letting survivor run."
                )
                continue

            pick = _find_repair_strike(spot_close, iv, dte, r, side, target_px, self.cfg)
            if not pick:
                # No quality match in ±tolerance → SKIP repair (v1.1 rule).
                jodi.repair_pending_side = None
                jodi.repair_ready_at = None
                jodi.status = "one_side_dead"
                self.output.events.append(
                    f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} SKIP REPAIR — "
                    f"no {side} strike within ±₹{max(self.cfg.repair_tolerance_abs, target_px*self.cfg.repair_tolerance_pct/100):.1f} of ₹{target_px:.1f}. Survivor runs."
                )
                continue
            new_strike, new_px = pick

            new_sum = target_px + new_px
            if new_sum < self.cfg.min_combined_premium:
                # Sum too low → skip repair.
                jodi.repair_pending_side = None
                jodi.repair_ready_at = None
                jodi.status = "one_side_dead"
                self.output.events.append(
                    f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} SKIP REPAIR — "
                    f"combined ₹{new_sum:.1f} < min ₹{self.cfg.min_combined_premium:.0f}."
                )
                continue

            leg = Leg(side, "short", new_strike, bar_time, new_px, qty=self.qty_per_leg)
            if side == "PE":
                jodi.short_pe = leg
            else:
                jodi.short_ce = leg
            jodi.repairs_used += 1
            jodi.repair_pending_side = None
            jodi.repair_ready_at = None
            jodi.sum_short = new_sum
            jodi.per_leg_sl = jodi_sl_from_sum(jodi.sum_short)
            jodi.profit_target = self.cfg.profit_book_pct * jodi.sum_short
            self.output.events.append(
                f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} REPAIR {side} → {new_strike}@{new_px:.1f} "
                f"(mirror ₹{target_px:.1f} · stable: {' + '.join(reasons)}) "
                f"repair {jodi.repairs_used}/{self.cfg.max_repairs_per_jodi} new sum={jodi.sum_short:.1f} → SL/leg=₹{jodi.per_leg_sl:.0f}"
            )

    def _maybe_close_hedges_and_complete(self, jodi: Jodi, bar_time: datetime, spot: float, iv: float, dte: float, r: float, status: str):
        # If BOTH shorts are closed, close hedges too and mark Jodi complete.
        if not jodi.short_pe.is_open and not jodi.short_ce.is_open:
            if jodi.long_pe.is_open:
                lpx = _bs_price(spot, jodi.long_pe.strike, iv, dte, "PE", r)
                self._close_leg(jodi.long_pe, bar_time, lpx, "HEDGE_CLOSE")
            if jodi.long_ce.is_open:
                lpx = _bs_price(spot, jodi.long_ce.strike, iv, dte, "CE", r)
                self._close_leg(jodi.long_ce, bar_time, lpx, "HEDGE_CLOSE")
            jodi.status = status
            self._book_jodi(jodi)

    def _settle_expiry(self, jodi: Jodi, spot_close: float, iv, r, bar_time: datetime):
        # Intrinsic settlement for any legs still open.
        for leg in jodi.all_legs():
            if not leg.is_open:
                continue
            intrinsic = max(0.0, spot_close - leg.strike) if leg.side == "CE" else max(0.0, leg.strike - spot_close)
            self._close_leg(leg, bar_time, intrinsic, "EXPIRY")
        jodi.status = "expired"
        self._book_jodi(jodi)
        self.output.events.append(
            f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} EXPIRY settled at spot={spot_close:.0f}  "
            f"net_pnl ₹{jodi.booked_pnl:,.0f}"
        )

    def _force_close_all(self, bar, vix_by_date, expiries_map, reason: str):
        spot = bar["close"]
        iv = vix_by_date.get(bar["date"].date(), 15.0)
        for jodi in self.jodis:
            if jodi.status in ("profit_booked", "expired", "abandoned"):
                continue
            dte = max(
                (datetime.combine(jodi.expiry, time(15, 30)) - bar["date"]).total_seconds() / 86400,
                0.02,
            )
            for leg in jodi.all_legs():
                if leg.is_open:
                    px = _bs_price(spot, leg.strike, iv, dte, leg.side, self.cfg.risk_free_rate)
                    self._close_leg(leg, bar["date"], px, reason.upper())
            jodi.status = "expired" if reason.startswith("hard") else "abandoned"
            self._book_jodi(jodi)

    def _close_leg(self, leg: Leg, exit_time: datetime, exit_price: float, reason: str):
        leg.exit_time = exit_time
        leg.exit_price = round(max(0.0, exit_price), 2)
        leg.is_open = False
        leg.exit_reason = reason
        self.output.trades.append({
            "entry_date": leg.entry_time.isoformat(),
            "exit_date":  exit_time.isoformat(),
            "direction":  leg.direction,
            "side":       leg.side,
            "strike":     leg.strike,
            "entry_price": round(leg.entry_price, 2),
            "exit_price":  round(leg.exit_price, 2),
            "quantity":    leg.qty,
            "pnl":         round(leg.realized_pnl, 2),
            "note":        f"{reason} · {leg.direction.upper()} {leg.side} {leg.strike}",
        })

    def _book_jodi(self, jodi: Jodi):
        # Sum realized P&L across all legs.
        total = sum(l.realized_pnl for l in jodi.all_legs())
        jodi.booked_pnl = total
        self.equity += total
        self.day_realized += total
        # Append Jodi summary
        self.output.jodis.append({
            "id": jodi.id,
            "entry_time": jodi.entry_time.isoformat(),
            "expiry": jodi.expiry.isoformat(),
            "status": jodi.status,
            "repairs": jodi.repairs_used,
            "sum_short_entry": round(jodi.sum_short, 2),
            "per_leg_sl": round(jodi.per_leg_sl, 2),
            "short_pe_strike": jodi.short_pe.strike,
            "short_ce_strike": jodi.short_ce.strike,
            "long_pe_strike": jodi.long_pe.strike,
            "long_ce_strike": jodi.long_ce.strike,
            "pnl": round(total, 2),
        })

    def _daily_loss_hit(self) -> bool:
        loss_pct = -(self.day_realized) / max(1, self.day_start_equity) * 100
        return loss_pct >= self.cfg.daily_loss_limit_pct

    def _record_equity(self, bar_time: datetime,
                       mtm_spot: Optional[float] = None, iv: Optional[float] = None,
                       r: Optional[float] = None, expiry: Optional[date] = None):
        mtm = 0.0
        if mtm_spot is not None:
            for jodi in self.jodis:
                if jodi.status not in ("active", "one_side_dead"):
                    continue
                dte = max((datetime.combine(jodi.expiry, time(15, 30)) - bar_time).total_seconds() / 86400, 0.02)
                for leg in jodi.all_legs():
                    if not leg.is_open:
                        continue
                    px = _bs_price(mtm_spot, leg.strike, iv, dte, leg.side, r)
                    sign = 1 if leg.direction == "short" else -1
                    mtm += sign * (leg.entry_price - px) * leg.qty
        self.output.equity_curve.append((bar_time.isoformat(), round(self.equity + mtm, 2)))

    # ---------------------------------------------------------------------
    # Data
    # ---------------------------------------------------------------------

    def _fetch_data(self, start: date, end: date) -> Tuple[List[Dict], Dict[date, float], str]:
        NIFTY_TOKEN = 256265
        VIX_TOKEN = 264969
        # Try 15-min first, else fall back to 5-min, else day
        bars = []
        granularity = "none"
        for interval in ("15minute", "5minute", "day"):
            try:
                bars = self.kite.historical_data(
                    NIFTY_TOKEN,
                    datetime.combine(start, time(0, 0)),
                    datetime.combine(end,   time(23, 59)),
                    interval,
                )
                if bars:
                    granularity = interval
                    break
            except Exception as e:
                logger.warning("Historical fetch @ %s failed: %s", interval, str(e)[:120])
                continue
        if not bars:
            return [], {}, "none"

        # Kite returns tz-aware datetimes (IST). Convert all to naive local time.
        for c in bars:
            if c["date"].tzinfo is not None:
                c["date"] = c["date"].replace(tzinfo=None)

        # VIX daily
        try:
            vix = self.kite.historical_data(
                VIX_TOKEN,
                datetime.combine(start, time(0, 0)),
                datetime.combine(end,   time(23, 59)),
                "day",
            )
            vix_by_date = {}
            for c in vix:
                d = c["date"].date() if hasattr(c["date"], "date") else c["date"]
                vix_by_date[d] = float(c["close"])
        except Exception:
            vix_by_date = {}
        return bars, vix_by_date, granularity


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _next_expiry(d: date, tuesdays: List[date]) -> Optional[date]:
    for t in tuesdays:
        if t >= d:
            return t
    return None


# ---------------------------------------------------------------------------
# Analytics — everything the spec asks for (Section 19)
# ---------------------------------------------------------------------------

def compute_analytics(engine_output: EngineOutput, initial_capital: float,
                      start: date, end: date) -> Dict[str, Any]:
    trades = engine_output.trades
    jodis = engine_output.jodis
    curve = engine_output.equity_curve
    final_equity = curve[-1][1] if curve else initial_capital
    total_pnl = final_equity - initial_capital
    total_return_pct = total_pnl / initial_capital * 100 if initial_capital else 0

    # Basic aggregates
    completed = [j for j in jodis if j["status"] in ("profit_booked", "expired", "abandoned")]
    winning_jodis = [j for j in completed if j["pnl"] > 0]
    losing_jodis  = [j for j in completed if j["pnl"] <= 0]

    total_repairs = sum(j["repairs"] for j in jodis)
    avg_repairs   = total_repairs / len(jodis) if jodis else 0
    successful_repairs = sum(1 for j in jodis if j["repairs"] > 0 and j["status"] == "profit_booked")
    repair_success_pct = (successful_repairs / max(1, sum(1 for j in jodis if j["repairs"] > 0))) * 100

    sl_trades = [t for t in trades if "SL_HIT" in t["note"]]
    profit_trades = [t for t in trades if "PROFIT_BOOK" in t["note"]]

    # Premium stats
    all_shorts = [t for t in trades if t["direction"] == "short"]
    avg_prem_sold = statistics.mean([t["entry_price"] for t in all_shorts]) if all_shorts else 0
    avg_prem_captured = statistics.mean([t["entry_price"] - t["exit_price"] for t in all_shorts]) if all_shorts else 0

    # Streaks
    def _streak(items, cond):
        best = cur = 0
        for i in items:
            if cond(i):
                cur += 1; best = max(best, cur)
            else:
                cur = 0
        return best
    winning_streak = _streak(completed, lambda j: j["pnl"] > 0)
    losing_streak  = _streak(completed, lambda j: j["pnl"] <= 0)

    # Daily returns for Sharpe/Sortino
    daily_by_date: Dict[str, float] = {}
    prev_eq = initial_capital
    for ts, eq in curve:
        d = ts[:10]
        daily_by_date[d] = eq  # last equity of the day
    daily_returns = []
    prev = initial_capital
    for d in sorted(daily_by_date.keys()):
        e = daily_by_date[d]
        daily_returns.append((e - prev) / prev if prev else 0)
        prev = e
    if daily_returns:
        mean_ret = statistics.mean(daily_returns)
        stdev_all = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0
        neg = [r for r in daily_returns if r < 0]
        stdev_neg = statistics.pstdev(neg) if len(neg) > 1 else 0
        sharpe  = (mean_ret / stdev_all * (252 ** 0.5)) if stdev_all else 0
        sortino = (mean_ret / stdev_neg * (252 ** 0.5)) if stdev_neg else 0
    else:
        sharpe = sortino = 0

    # Max drawdown
    peak = initial_capital; max_dd = 0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100)

    # Profit factor
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss   = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss else float("inf") if gross_profit else 0

    # Holding times
    def _hold_hours(j):
        et = datetime.fromisoformat(j["entry_time"])
        # Approximate exit as last trade of that jodi_id
        return None  # detailed compute skipped for brevity

    # Monthly heatmap (aggregate P&L per YYYY-MM)
    monthly: Dict[str, float] = {}
    for j in completed:
        m = j["entry_time"][:7]
        monthly[m] = monthly.get(m, 0.0) + j["pnl"]

    return {
        "total_jodis_created":       len(jodis),
        "total_jodis_completed":     len(completed),
        "total_jodis_active":        sum(1 for j in jodis if j["status"] in ("active", "one_side_dead")),
        "total_jodis_winning":       len(winning_jodis),
        "total_jodis_losing":        len(losing_jodis),
        "win_rate_pct":              round(len(winning_jodis) / len(completed) * 100, 2) if completed else 0,
        "loss_rate_pct":             round(len(losing_jodis) / len(completed) * 100, 2) if completed else 0,
        "total_repairs":             total_repairs,
        "avg_repairs_per_jodi":      round(avg_repairs, 2),
        "repair_success_pct":        round(repair_success_pct, 2),
        "jodis_abandoned":           sum(1 for j in jodis if j["status"] == "abandoned"),
        "sl_events":                 len(sl_trades),
        "profit_book_events":        len(profit_trades),
        "avg_premium_sold":          round(avg_prem_sold, 2),
        "avg_premium_captured":      round(avg_prem_captured, 2),
        "avg_pnl_per_jodi":          round(statistics.mean([j["pnl"] for j in completed]), 2) if completed else 0,
        "avg_profit_per_win":        round(statistics.mean([j["pnl"] for j in winning_jodis]), 2) if winning_jodis else 0,
        "avg_loss_per_loss":         round(statistics.mean([j["pnl"] for j in losing_jodis]), 2) if losing_jodis else 0,
        "max_winning_streak":        winning_streak,
        "max_losing_streak":         losing_streak,
        "sharpe_ratio":              round(sharpe, 3),
        "sortino_ratio":             round(sortino, 3),
        "profit_factor":             round(profit_factor, 3),
        "max_drawdown_pct":          round(max_dd, 3),
        "gross_profit":              round(gross_profit, 2),
        "gross_loss":                round(gross_loss, 2),
        "total_pnl":                 round(total_pnl, 2),
        "total_return_pct":          round(total_return_pct, 3),
        "initial_capital":           initial_capital,
        "final_capital":             round(final_equity, 2),
        "monthly_pnl":               {k: round(v, 2) for k, v in sorted(monthly.items())},
        "start_date":                start.isoformat(),
        "end_date":                  end.isoformat(),
    }


# ---------------------------------------------------------------------------
# Entry point used by the Flask route
# ---------------------------------------------------------------------------

def run_jodi_backtest(kite, start: date, end: date, capital: float,
                      cfg: Optional[JodiConfig] = None) -> Dict[str, Any]:
    cfg = cfg or JodiConfig()
    eng = JodiEngine(cfg, capital, kite)
    out = eng.run(start, end)
    stats = compute_analytics(out, capital, start, end)
    # Convert time objects → strings for JSON serialisation.
    cfg_dict = asdict(cfg)
    for k, v in cfg_dict.items():
        if isinstance(v, time):
            cfg_dict[k] = v.strftime("%H:%M")
    return {
        "strategy": "Jodi v1.0 · Continuous Theta Harvesting",
        "index": "NIFTY",
        "stats": stats,
        "trades": out.trades[:2000],
        "jodis":  out.jodis,
        "equity_curve": out.equity_curve[-2000:],
        "events": out.events[-300:],
        "config": cfg_dict,
    }
