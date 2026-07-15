"""Simplified backtest engine for the UI Trading System.

Fetches historical daily candles for a benchmark index via KiteConnect and
applies a proxy version of each strategy's core signal. Options-chain
history is not available on the dashboard's data plan, so option-selling
strategies are approximated with a rules-based premium model based on the
underlying's daily move. This is meant as a rough directional indicator,
not a precise P&L simulator.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)

# Kite instrument tokens for the underlying indices (well-known constants).
INDEX_TOKENS: Dict[str, Dict[str, object]] = {
    "NIFTY":     {"token": 256265,  "name": "NIFTY 50",      "lot": 65,  "strike_gap": 50},
    "BANKNIFTY": {"token": 260105,  "name": "NIFTY BANK",    "lot": 15,  "strike_gap": 100},
    "SENSEX":    {"token": 265,     "name": "SENSEX",        "lot": 10,  "strike_gap": 100},
}

INDIA_VIX_TOKEN = 264969  # NSE:INDIA VIX
JODI_MARGIN_PER_LOT = 200000.0  # ₹2L per condor lot as user specified
JODI_RISK_FREE_RATE = 6.5        # Annual %, standard for India


@dataclass
class Trade:
    """One completed trade in the simulation."""
    entry_date: str
    exit_date: str
    direction: str        # "long" or "short"
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    note: str = ""


@dataclass
class BacktestResult:
    strategy: str
    index: str
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
    best_trade_pnl: float
    worst_trade_pnl: float
    equity_curve: List[Dict[str, object]] = field(default_factory=list)
    trades: List[Dict[str, object]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _fetch_candles(kite: KiteConnect, index: str, start: date, end: date) -> List[Dict]:
    meta = INDEX_TOKENS[index]
    from_dt = datetime.combine(start, datetime.min.time())
    to_dt   = datetime.combine(end,   datetime.min.time())
    return kite.historical_data(meta["token"], from_dt, to_dt, "day")


def _finalise(trades: List[Trade], initial_capital: float,
              equity_curve: List[Tuple[str, float]], notes: List[str],
              strategy: str, index: str, start: date, end: date) -> BacktestResult:
    total_pnl = sum(t.pnl for t in trades)
    final_capital = initial_capital + total_pnl
    total_return_pct = (total_pnl / initial_capital) * 100.0 if initial_capital else 0.0
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    win_rate = (wins / len(trades) * 100.0) if trades else 0.0

    # Max drawdown from the equity curve.
    peak = initial_capital
    max_dd = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            max_dd = max(max_dd, dd)

    best  = max((t.pnl for t in trades), default=0.0)
    worst = min((t.pnl for t in trades), default=0.0)

    return BacktestResult(
        strategy=strategy,
        index=index,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        initial_capital=round(initial_capital, 2),
        final_capital=round(final_capital, 2),
        total_pnl=round(total_pnl, 2),
        total_return_pct=round(total_return_pct, 3),
        num_trades=len(trades),
        winning_trades=wins,
        losing_trades=losses,
        win_rate_pct=round(win_rate, 2),
        max_drawdown_pct=round(max_dd, 3),
        best_trade_pnl=round(best, 2),
        worst_trade_pnl=round(worst, 2),
        equity_curve=[{"date": d, "equity": round(e, 2)} for d, e in equity_curve],
        trades=[asdict(t) for t in trades],
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _strategy_buy_and_hold(candles: List[Dict], capital: float, lot: int) -> Tuple[List[Trade], List[Tuple[str, float]], List[str]]:
    """Baseline reference — buy the index at first close, sell at last close."""
    if len(candles) < 2:
        return [], [], ["Not enough data."]

    entry_price = candles[0]["close"]
    qty = max(lot, int(capital / entry_price // lot) * lot)
    equity_curve = []
    running_pnl = 0.0
    for c in candles:
        running_pnl = (c["close"] - entry_price) * qty
        equity_curve.append((c["date"].isoformat()[:10], capital + running_pnl))

    exit_price = candles[-1]["close"]
    pnl = (exit_price - entry_price) * qty
    t = Trade(
        entry_date=candles[0]["date"].isoformat()[:10],
        exit_date=candles[-1]["date"].isoformat()[:10],
        direction="long",
        entry_price=round(entry_price, 2),
        exit_price=round(exit_price, 2),
        quantity=qty,
        pnl=round(pnl, 2),
        note="Buy at start of range, hold to end.",
    )
    return [t], equity_curve, [f"Position sized to {qty} units at open (~{qty * entry_price / capital * 100:.1f}% of capital)."]


def _strategy_wave_extractor(candles: List[Dict], capital: float, lot: int) -> Tuple[List[Trade], List[Tuple[str, float]], List[str]]:
    """Buy 0.5% dips, sell 0.5% pops on the underlying. Proxy for wave logic."""
    trades: List[Trade] = []
    equity = capital
    equity_curve: List[Tuple[str, float]] = []
    position_qty = 0
    entry_px = 0.0
    entry_dt: Optional[str] = None
    qty_per_wave = max(lot, int(capital * 0.10 / candles[0]["close"] // lot) * lot)

    for i, c in enumerate(candles):
        if i == 0:
            equity_curve.append((c["date"].isoformat()[:10], equity))
            prev_close = c["close"]
            continue
        low, high, close = c["low"], c["high"], c["close"]
        dip_trigger = prev_close * 0.995
        pop_trigger = prev_close * 1.005

        if position_qty == 0 and low <= dip_trigger:
            position_qty = qty_per_wave
            entry_px = dip_trigger
            entry_dt = c["date"].isoformat()[:10]
        elif position_qty > 0 and high >= pop_trigger:
            exit_px = pop_trigger
            pnl = (exit_px - entry_px) * position_qty
            equity += pnl
            trades.append(Trade(
                entry_date=entry_dt, exit_date=c["date"].isoformat()[:10],
                direction="long", entry_price=round(entry_px, 2),
                exit_price=round(exit_px, 2), quantity=position_qty,
                pnl=round(pnl, 2), note="Bought 0.5% dip, sold 0.5% pop",
            ))
            position_qty = 0
            entry_px = 0.0

        # Mark-to-market equity for the chart
        mtm = (close - entry_px) * position_qty if position_qty else 0.0
        equity_curve.append((c["date"].isoformat()[:10], equity + mtm))
        prev_close = close

    # Close any open position at the last close.
    if position_qty > 0:
        exit_px = candles[-1]["close"]
        pnl = (exit_px - entry_px) * position_qty
        equity += pnl
        trades.append(Trade(
            entry_date=entry_dt, exit_date=candles[-1]["date"].isoformat()[:10],
            direction="long", entry_price=round(entry_px, 2),
            exit_price=round(exit_px, 2), quantity=position_qty,
            pnl=round(pnl, 2), note="Closed at end-of-range (open wave)",
        ))

    return trades, equity_curve, [
        f"Wave size: 0.5% dips/pops on daily bars. Qty per wave: {qty_per_wave}.",
    ]


def _strategy_survivor(candles: List[Dict], capital: float, lot: int) -> Tuple[List[Trade], List[Tuple[str, float]], List[str]]:
    """Weekly short-straddle proxy: sell ATM straddle Mon, buy back Fri."""
    trades: List[Trade] = []
    equity = capital
    equity_curve: List[Tuple[str, float]] = []
    week_entry_close: Optional[float] = None
    week_entry_date: Optional[str] = None
    # 1% of spot as combined straddle premium (typical for weekly ATM).
    premium_pct = 0.01
    contracts = 1  # 1 lot per week — kept simple

    for c in candles:
        d = c["date"]
        weekday = d.weekday()  # 0=Mon
        close = c["close"]
        date_iso = d.isoformat()[:10]

        if weekday == 0 and week_entry_close is None:
            # Sell ATM straddle at Monday's close.
            week_entry_close = close
            week_entry_date = date_iso
        elif weekday >= 3 and week_entry_close is not None:
            # Buy back at Thursday/Friday close.
            move = abs(close - week_entry_close) / week_entry_close
            entry_premium = week_entry_close * premium_pct
            # At expiry the straddle is worth max(0, |move|) — but for a proxy,
            # use residual = spot * 0.4 * premium_pct if untouched, or full
            # move value if move > premium. Short seller earns premium - move.
            residual_value = max(0.0, close * (move - premium_pct * 0.5))
            pnl_per_unit = entry_premium - residual_value
            pnl = pnl_per_unit * lot * contracts
            equity += pnl
            trades.append(Trade(
                entry_date=week_entry_date, exit_date=date_iso,
                direction="short", entry_price=round(week_entry_close, 2),
                exit_price=round(close, 2), quantity=lot * contracts,
                pnl=round(pnl, 2),
                note=f"Weekly straddle. Move {move*100:.2f}% vs premium {premium_pct*100:.2f}%",
            ))
            week_entry_close = None
            week_entry_date = None
        equity_curve.append((date_iso, equity))

    return trades, equity_curve, [
        "Proxy: sell 1-lot ATM straddle Mon close, buy back Thu/Fri close.",
        f"Assumed straddle premium: {premium_pct*100:.1f}% of spot.",
    ]


def _strategy_expiry_trade(candles: List[Dict], capital: float, lot: int) -> Tuple[List[Trade], List[Tuple[str, float]], List[str]]:
    """Momentum proxy — daily RSI(14) crossovers.

    Real expiry-trade module uses 3-min Stoch RSI; here we use daily RSI as a
    proxy for the underlying trend direction.
    """
    if len(candles) < 20:
        return [], [], ["Need at least 20 candles for RSI-based backtest."]

    closes = [c["close"] for c in candles]
    n = 14
    # Wilder RSI
    rsi_vals: List[float] = []
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        rs = (avg_gain / avg_loss) if avg_loss else 999
        rsi_vals.append(100 - (100 / (1 + rs)))
    # Align rsi_vals to candles[n+1:]

    trades: List[Trade] = []
    equity = capital
    equity_curve: List[Tuple[str, float]] = []
    position_qty = 0
    entry_px = 0.0
    entry_dt: Optional[str] = None
    qty = max(lot, int(capital * 0.20 / closes[0] // lot) * lot)

    for i, c in enumerate(candles):
        d = c["date"].isoformat()[:10]
        if i >= n + 1:
            rsi = rsi_vals[i - n - 1]
            close = c["close"]
            if position_qty == 0 and rsi < 30:
                position_qty = qty
                entry_px = close
                entry_dt = d
            elif position_qty > 0 and rsi > 70:
                exit_px = close
                pnl = (exit_px - entry_px) * position_qty
                equity += pnl
                trades.append(Trade(
                    entry_date=entry_dt, exit_date=d, direction="long",
                    entry_price=round(entry_px, 2), exit_price=round(exit_px, 2),
                    quantity=position_qty, pnl=round(pnl, 2),
                    note=f"RSI oversold → overbought (RSI={rsi:.1f})",
                ))
                position_qty = 0
                entry_px = 0.0
        mtm = (c["close"] - entry_px) * position_qty if position_qty else 0.0
        equity_curve.append((d, equity + mtm))

    if position_qty > 0:
        exit_px = candles[-1]["close"]
        pnl = (exit_px - entry_px) * position_qty
        equity += pnl
        trades.append(Trade(
            entry_date=entry_dt, exit_date=candles[-1]["date"].isoformat()[:10],
            direction="long", entry_price=round(entry_px, 2),
            exit_price=round(exit_px, 2), quantity=position_qty,
            pnl=round(pnl, 2), note="Closed at end-of-range (open position)",
        ))

    return trades, equity_curve, [
        "Proxy uses daily RSI(14): long when RSI < 30, exit when RSI > 70.",
        "Real module uses 3-min Stoch RSI on expiry day — a much shorter horizon.",
    ]


def _strategy_covered_calls(candles: List[Dict], capital: float, lot: int) -> Tuple[List[Trade], List[Tuple[str, float]], List[str]]:
    """Weekly OTM call selling proxy on the underlying index.

    Assumes: long the index (buy-and-hold), plus each Monday sell a 2% OTM
    weekly call priced at 0.5% of spot. If Friday close breaches strike,
    forfeit the excess; else pocket the premium.
    """
    if len(candles) < 5:
        return [], [], ["Need at least 5 candles."]

    trades: List[Trade] = []
    equity = capital
    equity_curve: List[Tuple[str, float]] = []

    # Long the underlying (buy-and-hold portion).
    initial_close = candles[0]["close"]
    qty = max(lot, int(capital * 0.60 / initial_close // lot) * lot)

    week_strike: Optional[float] = None
    week_premium: Optional[float] = None
    week_entry_date: Optional[str] = None

    for c in candles:
        d = c["date"]
        date_iso = d.isoformat()[:10]
        weekday = d.weekday()
        close = c["close"]

        if weekday == 0 and week_strike is None:
            week_strike = close * 1.02
            week_premium = close * 0.005
            week_entry_date = date_iso
        elif weekday >= 3 and week_strike is not None:
            # Settle the sold call.
            if close > week_strike:
                loss_leg = (close - week_strike) * lot
                pnl = (week_premium * lot) - loss_leg
                note = f"Call assigned. Strike {week_strike:.0f} < close {close:.0f}"
            else:
                pnl = week_premium * lot
                note = f"Call expired worthless. Strike {week_strike:.0f} ≥ close {close:.0f}"
            equity += pnl
            trades.append(Trade(
                entry_date=week_entry_date, exit_date=date_iso,
                direction="short", entry_price=round(week_strike, 2),
                exit_price=round(close, 2), quantity=lot,
                pnl=round(pnl, 2), note=note,
            ))
            week_strike = None
            week_premium = None

        # Equity = premium P&L accumulated + mark-to-market of long stock leg.
        stock_mtm = (close - initial_close) * qty
        equity_curve.append((date_iso, equity + stock_mtm))

    # Add the buy-and-hold leg's realized P&L at the very end (only if range covered).
    final_close = candles[-1]["close"]
    stock_pnl = (final_close - initial_close) * qty
    equity += stock_pnl
    trades.append(Trade(
        entry_date=candles[0]["date"].isoformat()[:10],
        exit_date=candles[-1]["date"].isoformat()[:10],
        direction="long", entry_price=round(initial_close, 2),
        exit_price=round(final_close, 2), quantity=qty,
        pnl=round(stock_pnl, 2),
        note="Underlying leg (buy-and-hold portion of covered call).",
    ))
    # Replace the final equity point with the fully-settled value.
    if equity_curve:
        equity_curve[-1] = (equity_curve[-1][0], equity)

    return trades, equity_curve, [
        "Proxy: buy underlying (60% of capital) + sell weekly 2% OTM calls.",
        "Assumed call premium: 0.5% of spot.",
    ]


STRATEGIES: Dict[str, Tuple[str, Callable]] = {
    "buy_and_hold":    ("Buy & Hold (baseline)",       _strategy_buy_and_hold),
    "wave_extractor":  ("Wave Extractor (0.5% waves)", _strategy_wave_extractor),
    "survivor":        ("Survivor (weekly straddle)",  _strategy_survivor),
    "expiry_trade":    ("Expiry Trade (RSI proxy)",    _strategy_expiry_trade),
    "covered_calls":   ("Covered Calls (weekly OTM)",  _strategy_covered_calls),
    "jodi":            ("Jodi (NIFTY iron-condor, Mon/Fri entry)", None),  # handled separately
}


# ---------------------------------------------------------------------------
# Jodi Strategy — NIFTY iron condor with Mon/Fri entry, Tue expiry
# ---------------------------------------------------------------------------

def _jodi_sl(sum_premium: float) -> float:
    """Jodi SL rounding:
      * last digit 1-5   → tens*10 + 5   (round UP to next X5)
      * last digit 0,6-9 → (tens+1)*10 + 1 (jump to next-decade + 1)
    """
    s = int(round(sum_premium))
    last = s % 10
    base = s - last
    if last in (1, 2, 3, 4, 5):
        return float(base + 5)
    return float(base + 11)


def _bs_option_price(spot: float, strike: float, iv_pct: float, dte_days: float,
                      option_type: str, r_pct: float = JODI_RISK_FREE_RATE) -> float:
    """Black-Scholes price via mibian. `dte_days` is calendar days remaining."""
    try:
        import mibian
    except ImportError:
        return 0.0
    dte = max(dte_days, 0.5)  # avoid zero-DTE math issues
    bs = mibian.BS([spot, strike, r_pct, dte], volatility=max(iv_pct, 1.0))
    px = bs.callPrice if option_type == "CE" else bs.putPrice
    return max(0.05, float(px))


def _nse_tuesday_expiries(start: date, end: date, holidays: set) -> List[date]:
    """List of NIFTY weekly Tuesday expiries between start & end (holiday-adjusted to Monday)."""
    d = start
    out: List[date] = []
    while d <= end + timedelta(days=7):
        if d.weekday() == 1:  # Tuesday
            expiry = d
            if expiry in holidays:
                expiry = expiry - timedelta(days=1)  # roll to Monday
            if start <= expiry <= end:
                out.append(expiry)
        d += timedelta(days=1)
    return out


def _round_strike(price: float, gap: int) -> int:
    return int(round(price / gap) * gap)


class _JodiPosition:
    """One live condor leg-set."""
    def __init__(self, entry_date, spot, iv, expiry_date, gap, lots):
        self.entry_date = entry_date
        self.expiry_date = expiry_date
        self.spot_at_entry = spot
        self.iv_at_entry = iv
        self.lots = lots
        self.qty = lots * 65  # NIFTY lot

        dte = max((expiry_date - entry_date).days, 1)
        # On Tuesday (0-1 DTE) target strikes ≥250 pts OTM (premiums are low);
        # otherwise standard 500-pt OTM target.
        base_otm = 250 if dte <= 1 else 500
        wing_extra = 500  # wings always 500 pts further OTM

        self.short_pe_strike = _round_strike(spot - base_otm, gap)
        self.short_ce_strike = _round_strike(spot + base_otm, gap)
        self.long_pe_strike  = self.short_pe_strike - wing_extra
        self.long_ce_strike  = self.short_ce_strike + wing_extra

        self.short_pe_entry = _bs_option_price(spot, self.short_pe_strike, iv, dte, "PE")
        self.short_ce_entry = _bs_option_price(spot, self.short_ce_strike, iv, dte, "CE")
        self.long_pe_entry  = _bs_option_price(spot, self.long_pe_strike,  iv, dte, "PE")
        self.long_ce_entry  = _bs_option_price(spot, self.long_ce_strike,  iv, dte, "CE")

        self.sum_short = self.short_pe_entry + self.short_ce_entry
        self.sl = _jodi_sl(self.sum_short)
        self.target_short_sum = 0.20 * self.sum_short  # 80% decay target

        self.short_pe_open = True
        self.short_ce_open = True
        self.realized = 0.0

        self.log_entries: List[str] = [
            f"[{entry_date}] Spot={spot:.0f} IV={iv:.1f}% DTE={dte}d "
            f"SHORT PE {self.short_pe_strike}@{self.short_pe_entry:.1f} + "
            f"CE {self.short_ce_strike}@{self.short_ce_entry:.1f}  "
            f"BUY PE {self.long_pe_strike}@{self.long_pe_entry:.1f} + "
            f"CE {self.long_ce_strike}@{self.long_ce_entry:.1f}  "
            f"credit_sum={self.sum_short:.1f}  SL={self.sl:.0f}  target={self.target_short_sum:.1f}"
        ]

    def option_prices_at(self, spot, iv, dte):
        pe = _bs_option_price(spot, self.short_pe_strike, iv, dte, "PE") if self.short_pe_open else 0.0
        ce = _bs_option_price(spot, self.short_ce_strike, iv, dte, "CE") if self.short_ce_open else 0.0
        return pe, ce

    def is_fully_closed(self) -> bool:
        return not self.short_pe_open and not self.short_ce_open


def _jodi_run(kite: KiteConnect, start: date, end: date, capital: float) -> Tuple[List[Trade], List[Tuple[str, float]], List[str]]:
    from datetime import datetime as _dt
    lots = max(1, int(capital // JODI_MARGIN_PER_LOT))
    if lots < 1:
        raise ValueError(f"Capital ₹{capital:,.0f} < ₹2L margin required per condor lot.")
    qty_per_leg = lots * 65

    # Fetch NIFTY spot + India VIX daily candles.
    spot_candles = _fetch_candles(kite, "NIFTY", start, end)
    vix_data = kite.historical_data(INDIA_VIX_TOKEN,
                                     _dt.combine(start, _dt.min.time()),
                                     _dt.combine(end,   _dt.min.time()), "day")
    vix_by_date = {c["date"].date(): float(c["close"]) for c in vix_data}
    spot_by_date = {c["date"].date(): c for c in spot_candles}

    # Build holiday set (basic — NSE holidays for this year via `holidays` pkg if available).
    holidays_set: set = set()
    try:
        import holidays as _hol
        for y in range(start.year, end.year + 1):
            for d, _n in _hol.India(years=y).items():
                holidays_set.add(d)
    except Exception:
        pass

    trading_days = sorted(spot_by_date.keys())
    if not trading_days:
        raise ValueError("No NIFTY candles for the range.")

    tuesday_expiries = _nse_tuesday_expiries(start, end + timedelta(days=7), holidays_set)
    gap = int(INDEX_TOKENS["NIFTY"]["strike_gap"])

    trades: List[Trade] = []
    equity_curve: List[Tuple[str, float]] = []
    equity = capital
    log: List[str] = [f"NIFTY lot=65 · lots per condor={lots} · margin/lot=₹{JODI_MARGIN_PER_LOT:,.0f}"]
    position: Optional[_JodiPosition] = None

    def _next_expiry_after(d: date) -> Optional[date]:
        for e in tuesday_expiries:
            if e >= d:
                return e
        return None

    for day in trading_days:
        cndl = spot_by_date[day]
        open_p, high_p, low_p, close_p = cndl["open"], cndl["high"], cndl["low"], cndl["close"]
        weekday = day.weekday()  # Mon=0 ... Fri=4
        iv = vix_by_date.get(day, 15.0)

        # ---- Entry: Monday (0) or Friday (4) if no position open ----
        if position is None and weekday in (0, 4):
            expiry = _next_expiry_after(day + timedelta(days=1))  # next Tue expiry
            if expiry is None:
                equity_curve.append((day.isoformat(), equity))
                continue
            # Entry at ~9:30 → approximate with day's open.
            position = _JodiPosition(day, open_p, iv, expiry, gap, lots)
            log.extend(position.log_entries)

        # ---- Manage open position for the day ----
        if position is not None:
            dte_days = max((position.expiry_date - day).days, 0)

            # If today is expiry: settle at intrinsic value on close.
            if day >= position.expiry_date:
                # PE settles: max(0, K - S). CE settles: max(0, S - K).
                pe_close = max(0.0, position.short_pe_strike - close_p) if position.short_pe_open else 0.0
                ce_close = max(0.0, close_p - position.short_ce_strike) if position.short_ce_open else 0.0
                long_pe_close = max(0.0, position.long_pe_strike - close_p)
                long_ce_close = max(0.0, close_p - position.long_ce_strike)

                pnl_pe = (position.short_pe_entry - pe_close) * qty_per_leg if position.short_pe_open else 0.0
                pnl_ce = (position.short_ce_entry - ce_close) * qty_per_leg if position.short_ce_open else 0.0
                pnl_long_pe = (long_pe_close - position.long_pe_entry) * qty_per_leg
                pnl_long_ce = (long_ce_close - position.long_ce_entry) * qty_per_leg
                total = pnl_pe + pnl_ce + pnl_long_pe + pnl_long_ce + position.realized
                equity += total

                if position.short_pe_open:
                    trades.append(Trade(
                        entry_date=position.entry_date.isoformat(),
                        exit_date=day.isoformat(), direction="short",
                        entry_price=round(position.short_pe_entry, 2),
                        exit_price=round(pe_close, 2), quantity=qty_per_leg,
                        pnl=round(pnl_pe, 2),
                        note=f"SHORT PE {position.short_pe_strike} · settled at expiry",
                    ))
                if position.short_ce_open:
                    trades.append(Trade(
                        entry_date=position.entry_date.isoformat(),
                        exit_date=day.isoformat(), direction="short",
                        entry_price=round(position.short_ce_entry, 2),
                        exit_price=round(ce_close, 2), quantity=qty_per_leg,
                        pnl=round(pnl_ce, 2),
                        note=f"SHORT CE {position.short_ce_strike} · settled at expiry",
                    ))
                trades.append(Trade(
                    entry_date=position.entry_date.isoformat(),
                    exit_date=day.isoformat(), direction="long",
                    entry_price=round(position.long_pe_entry, 2),
                    exit_price=round(long_pe_close, 2), quantity=qty_per_leg,
                    pnl=round(pnl_long_pe, 2), note=f"LONG PE {position.long_pe_strike} · hedge wing",
                ))
                trades.append(Trade(
                    entry_date=position.entry_date.isoformat(),
                    exit_date=day.isoformat(), direction="long",
                    entry_price=round(position.long_ce_entry, 2),
                    exit_price=round(long_ce_close, 2), quantity=qty_per_leg,
                    pnl=round(pnl_long_ce, 2), note=f"LONG CE {position.long_ce_strike} · hedge wing",
                ))
                log.append(f"[{day}] EXPIRY close={close_p:.0f}  net_condor_pnl=₹{total:,.0f}  equity=₹{equity:,.0f}")
                position = None
                equity_curve.append((day.isoformat(), equity))
                continue

            # Intraday: compute combined short premium at day's high and low.
            pe_hi, ce_hi = position.option_prices_at(high_p, iv, dte_days)
            pe_lo, ce_lo = position.option_prices_at(low_p,  iv, dte_days)
            # PE goes UP when spot falls (low); CE goes UP when spot rises (high).
            max_pe = max(pe_hi, pe_lo)   # PE peak = day's low spot
            max_ce = max(ce_hi, ce_lo)   # CE peak = day's high spot
            min_pe = min(pe_hi, pe_lo)   # PE trough = day's high spot
            min_ce = min(ce_hi, ce_lo)   # CE trough = day's low spot
            worst_sum = max_pe + max_ce
            best_sum  = min_pe + min_ce
            eod_pe, eod_ce = position.option_prices_at(close_p, iv, dte_days)

            # ----- Check SL first (worst_sum > SL) -----
            if worst_sum >= position.sl and (position.short_pe_open or position.short_ce_open):
                # Determine which leg triggered SL — the one that's UP more relative to entry.
                pe_gain = max_pe - position.short_pe_entry if position.short_pe_open else -1e9
                ce_gain = max_ce - position.short_ce_entry if position.short_ce_open else -1e9
                if pe_gain > ce_gain and position.short_pe_open:
                    # Exit losing PE short at ~max_pe. Re-enter new PE short near surviving CE's LTP.
                    exit_px = max_pe
                    pnl = (position.short_pe_entry - exit_px) * qty_per_leg
                    position.realized += pnl
                    trades.append(Trade(
                        entry_date=position.entry_date.isoformat(),
                        exit_date=day.isoformat(), direction="short",
                        entry_price=round(position.short_pe_entry, 2),
                        exit_price=round(exit_px, 2), quantity=qty_per_leg,
                        pnl=round(pnl, 2),
                        note=f"SL HIT · SHORT PE {position.short_pe_strike} · exit at ₹{exit_px:.1f}",
                    ))
                    position.short_pe_open = False
                    log.append(f"[{day}] SL HIT PE side · exit₹{exit_px:.1f} · realised ₹{pnl:,.0f}")

                    # Re-entry: find new PE strike whose BS price ≈ current surviving CE price.
                    target_px = eod_ce
                    new_strike = _find_pe_strike_for_price(close_p, target_px, iv, dte_days, gap)
                    if new_strike:
                        new_pe_entry = _bs_option_price(close_p, new_strike, iv, dte_days, "PE")
                        position.short_pe_strike = new_strike
                        position.short_pe_entry = new_pe_entry
                        position.short_pe_open = True
                        # Recompute combined sum and SL for the surviving CE + new PE.
                        position.sum_short = new_pe_entry + eod_ce
                        position.sl = _jodi_sl(position.sum_short)
                        log.append(
                            f"[{day}] RE-ENTRY PE {new_strike}@{new_pe_entry:.1f} · "
                            f"new sum={position.sum_short:.1f} · new SL={position.sl:.0f}"
                        )
                elif position.short_ce_open:
                    exit_px = max_ce
                    pnl = (position.short_ce_entry - exit_px) * qty_per_leg
                    position.realized += pnl
                    trades.append(Trade(
                        entry_date=position.entry_date.isoformat(),
                        exit_date=day.isoformat(), direction="short",
                        entry_price=round(position.short_ce_entry, 2),
                        exit_price=round(exit_px, 2), quantity=qty_per_leg,
                        pnl=round(pnl, 2),
                        note=f"SL HIT · SHORT CE {position.short_ce_strike} · exit at ₹{exit_px:.1f}",
                    ))
                    position.short_ce_open = False
                    log.append(f"[{day}] SL HIT CE side · exit₹{exit_px:.1f} · realised ₹{pnl:,.0f}")
                    target_px = eod_pe
                    new_strike = _find_ce_strike_for_price(close_p, target_px, iv, dte_days, gap)
                    if new_strike:
                        new_ce_entry = _bs_option_price(close_p, new_strike, iv, dte_days, "CE")
                        position.short_ce_strike = new_strike
                        position.short_ce_entry = new_ce_entry
                        position.short_ce_open = True
                        position.sum_short = eod_pe + new_ce_entry
                        position.sl = _jodi_sl(position.sum_short)
                        log.append(
                            f"[{day}] RE-ENTRY CE {new_strike}@{new_ce_entry:.1f} · "
                            f"new sum={position.sum_short:.1f} · new SL={position.sl:.0f}"
                        )

            # ----- Check profit target (best_sum ≤ 20% of entry sum) -----
            elif best_sum <= position.target_short_sum and (position.short_pe_open or position.short_ce_open):
                exit_pe = min_pe
                exit_ce = min_ce
                pnl_pe = (position.short_pe_entry - exit_pe) * qty_per_leg if position.short_pe_open else 0.0
                pnl_ce = (position.short_ce_entry - exit_ce) * qty_per_leg if position.short_ce_open else 0.0
                total = pnl_pe + pnl_ce
                position.realized += total
                if position.short_pe_open:
                    trades.append(Trade(
                        entry_date=position.entry_date.isoformat(),
                        exit_date=day.isoformat(), direction="short",
                        entry_price=round(position.short_pe_entry, 2),
                        exit_price=round(exit_pe, 2), quantity=qty_per_leg,
                        pnl=round(pnl_pe, 2),
                        note=f"80% TARGET · SHORT PE {position.short_pe_strike} closed at ₹{exit_pe:.1f}",
                    ))
                if position.short_ce_open:
                    trades.append(Trade(
                        entry_date=position.entry_date.isoformat(),
                        exit_date=day.isoformat(), direction="short",
                        entry_price=round(position.short_ce_entry, 2),
                        exit_price=round(exit_ce, 2), quantity=qty_per_leg,
                        pnl=round(pnl_ce, 2),
                        note=f"80% TARGET · SHORT CE {position.short_ce_strike} closed at ₹{exit_ce:.1f}",
                    ))
                position.short_pe_open = False
                position.short_ce_open = False
                # Close wings too at their current BS value.
                long_pe_ltp = _bs_option_price(close_p, position.long_pe_strike, iv, dte_days, "PE")
                long_ce_ltp = _bs_option_price(close_p, position.long_ce_strike, iv, dte_days, "CE")
                pnl_lpe = (long_pe_ltp - position.long_pe_entry) * qty_per_leg
                pnl_lce = (long_ce_ltp - position.long_ce_entry) * qty_per_leg
                trades.append(Trade(
                    entry_date=position.entry_date.isoformat(),
                    exit_date=day.isoformat(), direction="long",
                    entry_price=round(position.long_pe_entry, 2),
                    exit_price=round(long_pe_ltp, 2), quantity=qty_per_leg,
                    pnl=round(pnl_lpe, 2), note=f"LONG PE {position.long_pe_strike} · closed with condor",
                ))
                trades.append(Trade(
                    entry_date=position.entry_date.isoformat(),
                    exit_date=day.isoformat(), direction="long",
                    entry_price=round(position.long_ce_entry, 2),
                    exit_price=round(long_ce_ltp, 2), quantity=qty_per_leg,
                    pnl=round(pnl_lce, 2), note=f"LONG CE {position.long_ce_strike} · closed with condor",
                ))
                equity += total + pnl_lpe + pnl_lce
                log.append(f"[{day}] 80% TARGET HIT · net ₹{total + pnl_lpe + pnl_lce:,.0f}  equity=₹{equity:,.0f}")
                position = None

        # Mark-to-market equity for the day.
        if position is not None:
            dte_days = max((position.expiry_date - day).days, 0)
            eod_pe, eod_ce = position.option_prices_at(close_p, iv, dte_days)
            long_pe_ltp = _bs_option_price(close_p, position.long_pe_strike, iv, dte_days, "PE")
            long_ce_ltp = _bs_option_price(close_p, position.long_ce_strike, iv, dte_days, "CE")
            mtm = (
                (position.short_pe_entry - eod_pe) * qty_per_leg * (1 if position.short_pe_open else 0)
                + (position.short_ce_entry - eod_ce) * qty_per_leg * (1 if position.short_ce_open else 0)
                + (long_pe_ltp - position.long_pe_entry) * qty_per_leg
                + (long_ce_ltp - position.long_ce_entry) * qty_per_leg
                + position.realized
            )
            equity_curve.append((day.isoformat(), equity + mtm))
        else:
            equity_curve.append((day.isoformat(), equity))

    notes = [
        f"NIFTY iron condor · {lots} lot(s) · ₹{JODI_MARGIN_PER_LOT:,.0f} margin/lot · qty/leg={qty_per_leg}",
        "Entry: Monday (for that Tue expiry) & Friday (for next Tue expiry) at ~9:30 AM open.",
        "Shorts ~500 pts OTM (250 pts on expiry-day re-entry). Wings 500 pts further OTM.",
        "SL = combined short premium rounded UP: last-digit 1-5 → X5, else (X+1)1.",
        "On SL hit → close losing short + wing kept intact → re-short at strike mirroring surviving side.",
        "Exit on 80% decay of combined premium OR expiry settlement.",
        "Premiums synthesised via Black-Scholes using NIFTY spot + India VIX (mibian).",
    ]
    return trades, equity_curve, notes + log[-80:]  # cap log to last 80 events


def _find_pe_strike_for_price(spot: float, target_px: float, iv: float, dte: float, gap: int) -> Optional[int]:
    """Find a PE strike whose BS price is closest to target_px."""
    if target_px <= 0.5: return None
    best_strike = None
    best_diff = 1e9
    center = _round_strike(spot, gap)
    for k in range(center - 30 * gap, center + 5 * gap, gap):
        if k <= 0: continue
        px = _bs_option_price(spot, k, iv, dte, "PE")
        diff = abs(px - target_px)
        if diff < best_diff:
            best_diff = diff
            best_strike = k
    return best_strike


def _find_ce_strike_for_price(spot: float, target_px: float, iv: float, dte: float, gap: int) -> Optional[int]:
    if target_px <= 0.5: return None
    best_strike = None
    best_diff = 1e9
    center = _round_strike(spot, gap)
    for k in range(center - 5 * gap, center + 30 * gap, gap):
        if k <= 0: continue
        px = _bs_option_price(spot, k, iv, dte, "CE")
        diff = abs(px - target_px)
        if diff < best_diff:
            best_diff = diff
            best_strike = k
    return best_strike


def run_backtest(
    kite: KiteConnect,
    strategy_key: str,
    index: str,
    start: date,
    end: date,
    capital: float,
) -> BacktestResult:
    if strategy_key not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{strategy_key}'.")
    if index not in INDEX_TOKENS:
        raise ValueError(f"Unknown index '{index}'.")
    if start >= end:
        raise ValueError("Start date must be before end date.")

    label, fn = STRATEGIES[strategy_key]

    # Jodi is NIFTY-only and requires VIX + expiry calendar → dedicated path.
    if strategy_key == "jodi":
        if index != "NIFTY":
            raise ValueError("Jodi strategy is currently NIFTY-only.")
        trades, equity_curve, notes = _jodi_run(kite, start, end, capital)
        return _finalise(trades, capital, equity_curve, notes, label, index, start, end)

    candles = _fetch_candles(kite, index, start, end)
    if not candles:
        raise ValueError("No candle data returned by Kite for this range.")

    lot = int(INDEX_TOKENS[index]["lot"])
    trades, equity_curve, notes = fn(candles, capital, lot)
    result = _finalise(trades, capital, equity_curve, notes, label, index, start, end)
    return result
