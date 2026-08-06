"""Backtest for the CAS ATM±1 premium-capture strategy.

Uses daily index candles from Kite (when available) or synthetic paths.
On each expiry day the model:

1. Treats the day's close as the CAS equilibrium price.
2. Sells ATM+1 CE and ATM-1 PE at a Black-Scholes premium with ~minutes of
   time left (high theta / collapsing IV path).
3. Settles both legs to intrinsic at the same close (expiry settlement).

This is a directional proxy — real CAS microstructure and live option LTPs
are not available on the standard Kite historical plan.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from cas_expiry.strikes import INDEX_META, round_atm, target_strikes

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    entry_date: str
    exit_date: str
    index: str
    close_price: float
    atm: int
    ce_strike: int
    pe_strike: int
    ce_premium: float
    pe_premium: float
    ce_settlement: float
    pe_settlement: float
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


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float,
    strike: float,
    iv: float,
    days: float,
    opt_type: str,
    rate: float = 0.065,
) -> float:
    """Black-Scholes option price. ``days`` may be fractional."""
    t = max(days, 1e-6) / 365.0
    if spot <= 0 or strike <= 0 or iv <= 0:
        return max(0.0, (spot - strike) if opt_type == "CE" else (strike - spot))
    vol = iv / 100.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if opt_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
    return strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def intrinsic(spot: float, strike: float, opt_type: str) -> float:
    if opt_type == "CE":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def _nifty_expiry_dates(start: date, end: date) -> List[date]:
    """Weekly NIFTY expiries — Tuesday (post-2025 convention), skip weekends."""
    out: List[date] = []
    d = start
    while d <= end:
        if d.weekday() == 1:  # Tuesday
            out.append(d)
        d += timedelta(days=1)
    return out


def _sensex_expiry_dates(start: date, end: date) -> List[date]:
    """Weekly SENSEX expiries — Thursday."""
    out: List[date] = []
    d = start
    while d <= end:
        if d.weekday() == 3:  # Thursday
            out.append(d)
        d += timedelta(days=1)
    return out


def _fetch_daily_closes(
    kite: Any, index: str, start: date, end: date
) -> Dict[date, float]:
    meta = INDEX_META[index]
    candles = kite.historical_data(
        meta["token"],
        datetime.combine(start, datetime.min.time()),
        datetime.combine(end, datetime.min.time()),
        "day",
    )
    out: Dict[date, float] = {}
    for c in candles:
        dt = c["date"]
        if hasattr(dt, "date"):
            dt = dt.date()
        out[dt] = float(c["close"])
    return out


def run_cas_backtest(
    kite: Optional[Any],
    index: str = "NIFTY",
    start: date = None,
    end: date = None,
    capital: float = 500_000.0,
    lots: int = 1,
    ce_offset: int = 1,
    pe_offset: int = 1,
    assumed_iv: float = 18.0,
    entry_dte_minutes: float = 5.0,
) -> BacktestResult:
    """Run the CAS ATM±1 sell backtest.

    Args:
        kite: Authenticated KiteConnect (optional — synthetic path if None).
        index: NIFTY or SENSEX.
        start/end: Inclusive date range.
        capital: Starting capital.
        lots: Lots per leg.
        ce_offset/pe_offset: Strike steps from ATM.
        assumed_iv: Annualised IV % for entry premium.
        entry_dte_minutes: Minutes of time-value left when we sell (CAS window).
    """
    index = index.upper()
    if index not in INDEX_META:
        raise ValueError(f"Unsupported index {index}")
    meta = INDEX_META[index]
    gap = int(meta["strike_gap"])
    lot_size = int(meta["default_lot"])
    qty = lots * lot_size

    end = end or date.today()
    start = start or (end - timedelta(days=90))

    notes: List[str] = [
        "Proxy model: day's close = CAS equilibrium; legs settle to intrinsic at same close.",
        f"Entry premium = BS(IV={assumed_iv}%, T={entry_dte_minutes} minutes).",
        "Not a precise microstructure simulation — directional only.",
    ]

    closes: Dict[date, float] = {}
    if kite is not None:
        try:
            closes = _fetch_daily_closes(kite, index, start, end)
        except Exception as exc:
            notes.append(f"Kite historical fetch failed ({exc}); using synthetic path.")
            closes = {}

    if not closes:
        closes = _synthetic_closes(index, start, end)

    expiries = (
        _nifty_expiry_dates(start, end)
        if index == "NIFTY"
        else _sensex_expiry_dates(start, end)
    )

    trades: List[BacktestTrade] = []
    equity = float(capital)
    curve: List[Dict[str, object]] = [{"date": start.isoformat(), "equity": equity}]
    peak = equity
    max_dd = 0.0

    entry_days = entry_dte_minutes / (60.0 * 24.0)  # fraction of a day

    for exp in expiries:
        # Use expiry day's close; if missing (holiday), nearest prior close
        close_px = closes.get(exp)
        used = exp
        if close_px is None:
            for back in range(1, 5):
                alt = exp - timedelta(days=back)
                if alt in closes:
                    close_px = closes[alt]
                    used = alt
                    break
        if close_px is None:
            continue

        atm, ce_strike, pe_strike = target_strikes(
            close_px, gap, ce_offset, pe_offset
        )
        ce_prem = bs_price(close_px, ce_strike, assumed_iv, entry_days, "CE")
        pe_prem = bs_price(close_px, pe_strike, assumed_iv, entry_days, "PE")
        ce_set = intrinsic(close_px, ce_strike, "CE")
        pe_set = intrinsic(close_px, pe_strike, "PE")

        # Short premium: pnl = (entry_premium - settlement) * qty per leg
        pnl = ((ce_prem - ce_set) + (pe_prem - pe_set)) * qty
        equity += pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak else 0.0
        max_dd = max(max_dd, dd)

        trades.append(
            BacktestTrade(
                entry_date=used.isoformat(),
                exit_date=used.isoformat(),
                index=index,
                close_price=round(close_px, 2),
                atm=atm,
                ce_strike=ce_strike,
                pe_strike=pe_strike,
                ce_premium=round(ce_prem, 2),
                pe_premium=round(pe_prem, 2),
                ce_settlement=round(ce_set, 2),
                pe_settlement=round(pe_set, 2),
                quantity=qty,
                pnl=round(pnl, 2),
                note=f"ATM±{ce_offset} short strangle @ CAS close",
            )
        )
        curve.append({"date": used.isoformat(), "equity": round(equity, 2)})

    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    pnls = [t.pnl for t in trades]
    total_pnl = equity - capital

    return BacktestResult(
        strategy="cas_atm1_sell",
        index=index,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        initial_capital=capital,
        final_capital=round(equity, 2),
        total_pnl=round(total_pnl, 2),
        total_return_pct=round((total_pnl / capital) * 100.0, 4) if capital else 0.0,
        num_trades=len(trades),
        winning_trades=wins,
        losing_trades=losses,
        win_rate_pct=round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        max_drawdown_pct=round(max_dd, 4),
        best_trade_pnl=round(max(pnls), 2) if pnls else 0.0,
        worst_trade_pnl=round(min(pnls), 2) if pnls else 0.0,
        equity_curve=curve,
        trades=[asdict(t) for t in trades],
        notes=notes,
    )


def _synthetic_closes(index: str, start: date, end: date) -> Dict[date, float]:
    """Deterministic random-walk closes for offline / no-kite backtests."""
    import random

    rng = random.Random(42 + hash(index) % 1000)
    level = 24500.0 if index == "NIFTY" else 81000.0
    out: Dict[date, float] = {}
    d = start
    while d <= end:
        if d.weekday() < 5:
            level *= 1.0 + rng.uniform(-0.012, 0.012)
            out[d] = round(level, 2)
        d += timedelta(days=1)
    return out
