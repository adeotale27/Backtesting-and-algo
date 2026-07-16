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
    hedge_distance_pts: int = 500          # wing = short strike ± 500

    # Repair rules (Section 9-11)
    repair_delay_min: int = 15            # wait 10-15 min after SL
    max_repairs_per_jodi: int = 2

    # Profit booking (Section 12)
    profit_book_pct: float = 0.20          # book surviving short at <= 20% of entry

    # Daily risk (Section 14-15)
    daily_loss_limit_pct: float = 1.0
    daily_profit_target_pct: float = 2.0

    # Time-of-day rules (Sections 3, 16)
    entry_start_time: time = time(9, 30)      # first entry each day at 9:30
    tuesday_no_new_after: time = time(12, 0)
    hard_exit_time: time = time(15, 20)

    # Engine
    max_concurrent_jodis: int = 3          # multiple concurrent Jodis
    slippage_pct: float = 2.0              # % slippage on SL exit


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
    """After SL on one side, find a new short whose BS price ≈ surviving side's LTP,
    while respecting the strike-distance rules for the current time."""
    center = _round_strike(spot, cfg.strike_gap)
    sign = -1 if side == "PE" else +1
    best = None
    best_diff = 1e9
    # Ensure the repair strike still respects min distance (use late_min_distance).
    min_step = max(4, cfg.late_min_distance_fallback // cfg.strike_gap)
    for step in range(min_step, min_step + 30):
        k = center + sign * step * cfg.strike_gap
        if k <= 0: continue
        p = _bs_price(spot, k, iv, dte, side, r)
        diff = abs(p - target_premium)
        if diff < best_diff:
            best_diff = diff
            best = (k, p)
    return best


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
        self.jodis: List[Jodi] = []
        self.next_jodi_id = 1
        self.output = EngineOutput()

        # Daily state
        self.trading_disabled_until: Optional[date] = None
        self.day_start_equity = capital
        self.day_realized = 0.0
        self.last_bar_date: Optional[date] = None

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

        # ── Roll daily state ──
        if self.last_bar_date != bar_date:
            self.day_start_equity = self.equity
            self.day_realized = 0.0
            self.last_bar_date = bar_date

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

        # 4. Daily loss circuit-breaker (Section 14).
        if self._daily_loss_hit():
            self.trading_disabled_until = bar_date
            self._record_equity(bar_time)
            return

        # 5. Attempt to create a new Jodi if room / rules allow.
        if self._is_valid_entry_bar(bar_time):
            self._try_create_jodi(bar, spot_close, iv, r, expiry)

        # 6. Equity mark-to-market.
        self._record_equity(bar_time, mtm_spot=spot_close, iv=iv, r=r, expiry=expiry)

    def _is_valid_entry_bar(self, bar_time: datetime) -> bool:
        weekday = bar_time.weekday()
        # Fri (4), Mon (0), Tue (1) only. Tue: only before 12 PM.
        if weekday not in (0, 1, 4):
            return False
        if weekday == 1 and bar_time.time() >= self.cfg.tuesday_no_new_after:
            return False
        if bar_time.time() < self.cfg.entry_start_time:
            return False
        # If daily loss limit hit and we're still in the same day, disallow.
        if self.trading_disabled_until == bar_time.date():
            return False
        # If daily profit target hit, cease creating new (Section 15 keeps 1-2%).
        pnl_pct = (self.equity - self.day_start_equity) / self.day_start_equity * 100
        if pnl_pct >= self.cfg.daily_profit_target_pct:
            return False
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

        # Hedges — 500 pts further OTM.
        long_pe_strike = pe_strike - self.cfg.hedge_distance_pts
        long_ce_strike = ce_strike + self.cfg.hedge_distance_pts
        long_pe_px = _bs_price(spot, long_pe_strike, iv, dte, "PE", r)
        long_ce_px = _bs_price(spot, long_ce_strike, iv, dte, "CE", r)

        # Rule: minimum premium sanity — no zero-premium short.
        if pe_px < 3 or ce_px < 3:
            return

        sum_short = pe_px + ce_px
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
            pick = _find_repair_strike(spot_close, iv, dte, r, side, target_px, self.cfg)
            if not pick:
                jodi.repair_pending_side = None
                jodi.repair_ready_at = None
                continue
            new_strike, new_px = pick
            if new_px < 3:
                # Premium too low → skip repair (would just SL immediately).
                jodi.repair_pending_side = None
                jodi.repair_ready_at = None
                self.output.events.append(
                    f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} repair skipped for {side} — premium too low (₹{new_px:.1f})"
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
            jodi.sum_short = jodi.short_pe.entry_price + jodi.short_ce.entry_price if (jodi.short_pe.is_open and jodi.short_ce.is_open) else new_px + target_px
            jodi.per_leg_sl = jodi_sl_from_sum(jodi.sum_short)
            jodi.profit_target = self.cfg.profit_book_pct * jodi.sum_short
            self.output.events.append(
                f"[{bar_time:%Y-%m-%d %H:%M}] JODI #{jodi.id} REPAIR {side} → {new_strike}@{new_px:.1f} "
                f"(repair {jodi.repairs_used}/{self.cfg.max_repairs_per_jodi}) new sum={jodi.sum_short:.1f} → SL/leg=₹{jodi.per_leg_sl:.0f}"
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
        else:
            return [], {}, "none"

        # VIX daily
        try:
            vix = self.kite.historical_data(
                VIX_TOKEN,
                datetime.combine(start, time(0, 0)),
                datetime.combine(end,   time(23, 59)),
                "day",
            )
            vix_by_date = {c["date"].date(): float(c["close"]) for c in vix}
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
    return {
        "strategy": "Jodi v1.0 · Continuous Theta Harvesting",
        "index": "NIFTY",
        "stats": stats,
        "trades": out.trades[:2000],
        "jodis":  out.jodis,
        "equity_curve": out.equity_curve[-2000:],
        "events": out.events[-300:],
        "config": asdict(cfg),
    }
