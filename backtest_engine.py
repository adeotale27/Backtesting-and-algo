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
    "NIFTY":     {"token": 256265,  "name": "NIFTY 50",      "lot": 50},
    "BANKNIFTY": {"token": 260105,  "name": "NIFTY BANK",    "lot": 15},
    "SENSEX":    {"token": 265,     "name": "SENSEX",        "lot": 10},
}


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
}


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
    candles = _fetch_candles(kite, index, start, end)
    if not candles:
        raise ValueError("No candle data returned by Kite for this range.")

    lot = int(INDEX_TOKENS[index]["lot"])
    trades, equity_curve, notes = fn(candles, capital, lot)
    result = _finalise(trades, capital, equity_curve, notes, label, index, start, end)
    return result
