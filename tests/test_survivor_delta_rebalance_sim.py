"""Simulation: compare Survivor delta behavior before and after anchor rebalancing.

Run with:
    python tests/test_survivor_delta_rebalance_sim.py

Shows side-by-side how CE anchor stays frozen (old logic) vs. shifts after PE sells
(new logic), causing CE to trade much sooner when the market reverses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Simulation parameters — mimic a real Survivor start
# ---------------------------------------------------------------------------

CE_START: float = 23950.0
PE_START: float = 24100.0
PE_GAP: float = 18.0
CE_GAP: float = 18.0
PE_OTM_DISTANCE: float = 200.0  # OTM pts below spot for PE strike
CE_OTM_DISTANCE: float = 200.0  # OTM pts above spot for CE strike
VOLATILITY: float = 15.0        # annual IV %
INTEREST_RATE: float = 6.5      # %
LOT_SIZE: int = 65
LOTS_PER_SELL: int = 5
EXPIRY_DAYS_AT_START: float = 7.0  # DTE when simulation starts

# -----------------------------------------------------------------------
# Existing positions when the Survivor is initiated.
# These are the open short options already in the portfolio from prior
# sessions or other algos.  Paste your real positions here before running.
#
# Format: list of (strike, lots) — lots is always positive; the code
# treats them as short (sold) positions automatically.
#
# target_ce_delta and target_pe_delta are computed FROM these positions by
# evaluating the portfolio delta at CE_START and PE_START respectively.
# The rebalancer then finds the new anchor where the full portfolio
# (existing + session sells) produces that same delta again.
# -----------------------------------------------------------------------

# Short PE positions already open (strike, lots)
EXISTING_PE_POSITIONS: list[tuple[float, int]] = [
    (23500, 10),   # 5 lots short PE at 23500
    (23600, 10),   # 5 lots short PE at 23600
    (23700, 5),   # 5 lots short PE at 23600
    (23750, 5),   # 5 lots short PE at 23600
    (23800, 5),   # 5 lots short PE at 23800
]

# Short CE positions already open (strike, lots)
EXISTING_CE_POSITIONS: list[tuple[float, int]] = [
    (24050, 5),   # 5 lots short CE at 24300
    (24100, 5),   # 5 lots short CE at 24400
    (24150, 5),   # 5 lots short CE at 24400
    (24200, 10),   # 5 lots short CE at 24400
    (24250, 10),   # 5 lots short CE at 24400
    (24300, 10),   # 5 lots short CE at 24400
]

# Synthetic price series: rally from near PE_START then reversal
PRICE_SERIES: list[float] = [
    23950, 24000, 24100, 24120,  # approaching PE_START
    24130, 24140, 24150, 24160, 24170, 24180, 24190, 24200,   # T
    24220, 24225, 24205, 24195, 24180, 24170, 24160, 24150,   # 
    24160, 24170, 24180, 24190, 24200, 24210, 24220, 24230, 24240, 24250, 24260, 24270, 24280, 24290, 24300, 
    24290, 24280, 24270, 24260, 24250, 24240, 24230, 24220, 24210, 24200, 24190,  24180, 24170, 24160, 24150, 24140, 24130, 24120, 24100, 24090, 24080, 24070, 24060, 24050, 24040, 24030, 24020, 24010, 24000,
    23990,                               # new: CE fires here (shifted anchor); old: no
    23890,                               # new: CE fires again; old: still no
    23790,                               # old: CE fires here (original CE_START)
    23700,                               # below old CE anchor
]


# ---------------------------------------------------------------------------
# Minimal standalone Black-Scholes delta (no external dependency)
# ---------------------------------------------------------------------------


def _ncdf(x: float) -> float:
    """Cumulative standard normal distribution via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(
    spot: float,
    strike: float,
    days: float,
    volatility: float,
    interest_rate: float,
    option_type: str,
) -> float:
    """Black-Scholes delta for a European option.

    Args:
        spot: Underlying price.
        strike: Option strike.
        days: Days to expiry.
        volatility: Annual volatility in percent (e.g. 15 for 15%).
        interest_rate: Annual risk-free rate in percent.
        option_type: "CE" for call, "PE" for put.

    Returns:
        Delta (0–1 for call, −1–0 for put).
    """
    t = max(days / 365.0, 1e-6)
    v = volatility / 100.0
    r = interest_rate / 100.0
    d1 = (math.log(spot / strike) + (r + 0.5 * v * v) * t) / (v * math.sqrt(t))
    if option_type == "CE":
        return _ncdf(d1)
    return _ncdf(d1) - 1.0


# ---------------------------------------------------------------------------
# Portfolio delta helper
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """Single simulated options position."""

    strike: float
    option_type: str  # "CE" or "PE"
    quantity: int     # negative = short


def compute_portfolio_delta(
    positions: list[Position],
    spot: float,
    days_to_expiry: float,
) -> float:
    """Sum delta across all simulated positions at a given spot.

    Args:
        positions: List of open positions.
        spot: Hypothetical spot price.
        days_to_expiry: Time remaining to expiry in calendar days.

    Returns:
        Total net delta (per-share, summed across all lots).
    """
    total = 0.0
    for pos in positions:
        d = bs_delta(spot, pos.strike, days_to_expiry, VOLATILITY, INTEREST_RATE, pos.option_type)
        total += d * pos.quantity  # quantity negative for short → correct sign
    return total


def find_spot_for_target_delta(
    positions: list[Position],
    target_delta: float,
    search_center: float,
    days_to_expiry: float,
    search_half_width: float = 3000.0,
    tolerance: float = 1.0,
) -> Optional[float]:
    """Binary search: find spot where portfolio delta equals target_delta.

    Delta is monotonically decreasing with spot for short strangle portfolios.

    Args:
        positions: Current open positions.
        target_delta: Delta value to find the spot for.
        search_center: Centre of binary search range (typically current spot).
        days_to_expiry: DTE for delta calculation.
        search_half_width: Search ± this many points from centre.
        tolerance: Accept result when |computed_delta − target_delta| < tolerance.

    Returns:
        Spot float where delta ≈ target, or None if outside search range.
    """
    low = search_center - search_half_width
    high = search_center + search_half_width

    d_low = compute_portfolio_delta(positions, low, days_to_expiry)
    d_high = compute_portfolio_delta(positions, high, days_to_expiry)

    if not (min(d_low, d_high) <= target_delta <= max(d_low, d_high)):
        return None

    for _ in range(60):
        mid = (low + high) / 2.0
        d_mid = compute_portfolio_delta(positions, mid, days_to_expiry)
        if abs(d_mid - target_delta) < tolerance:
            return mid
        # Delta decreases with spot → if mid delta > target, move low up
        if d_mid > target_delta:
            low = mid
        else:
            high = mid

    return (low + high) / 2.0


# ---------------------------------------------------------------------------
# Core simulation runner
# ---------------------------------------------------------------------------


def run_simulation(use_rebalancing: bool) -> list[dict]:
    """Run the Survivor simulation on PRICE_SERIES.

    Args:
        use_rebalancing: If True, apply delta-preserving anchor rebalancing after
            each PE/CE sell. If False, anchors are never cross-updated (old behaviour).

    Returns:
        List of per-tick snapshot dicts for reporting.
    """
    label = "WITH rebalancing" if use_rebalancing else "WITHOUT rebalancing (baseline)"
    separator = "=" * 70
    print(f"\n{separator}\nSimulation {label}\n{separator}")

    pe_anchor: float = PE_START
    ce_anchor: float = CE_START
    snapshots: list[dict] = []

    # Build the initial portfolio from the user-supplied position arrays.
    # quantity is negative (short) — lots × LOT_SIZE shares per lot.
    positions: list[Position] = [
        Position(strike=strike, option_type="PE", quantity=-(lots * LOT_SIZE))
        for strike, lots in EXISTING_PE_POSITIONS
    ] + [
        Position(strike=strike, option_type="CE", quantity=-(lots * LOT_SIZE))
        for strike, lots in EXISTING_CE_POSITIONS
    ]

    # Compute target deltas: evaluate the existing portfolio at each anchor.
    # These are the reference delta levels the rebalancer tries to restore
    # after each sell on the opposite side.
    target_ce_delta: float = compute_portfolio_delta(positions, ce_anchor, EXPIRY_DAYS_AT_START)
    target_pe_delta: float = compute_portfolio_delta(positions, pe_anchor, EXPIRY_DAYS_AT_START)

    pe_desc = ", ".join(f"{s}×{l}L" for s, l in EXISTING_PE_POSITIONS) or "none"
    ce_desc = ", ".join(f"{s}×{l}L" for s, l in EXISTING_CE_POSITIONS) or "none"
    print(
        f"Init: ce_anchor={ce_anchor:.0f}  pe_anchor={pe_anchor:.0f}\n"
        f"      existing PE: {pe_desc}\n"
        f"      existing CE: {ce_desc}\n"
        f"      target_ce_delta={target_ce_delta:.2f} (delta at CE_START)  "
        f"target_pe_delta={target_pe_delta:.2f} (delta at PE_START)\n"
    )

    for tick_idx, spot in enumerate(PRICE_SERIES):
        # DTE decays slightly with each tick to reflect passage of time
        dte = max(EXPIRY_DAYS_AT_START - tick_idx * 0.15, 0.1)

        net_delta_before = compute_portfolio_delta(positions, spot, dte)
        events: list[str] = []

        # --- PE sell trigger: spot has risen above pe_anchor + pe_gap ---
        if spot > pe_anchor and (spot - pe_anchor) > PE_GAP:
            times = int((spot - pe_anchor) / PE_GAP)
            pe_anchor += PE_GAP * times
            for _ in range(times * LOTS_PER_SELL):
                sell_strike = round(spot - PE_OTM_DISTANCE, -2)  # round to nearest 100
                positions.append(Position(strike=sell_strike, option_type="PE", quantity=-LOT_SIZE))
            events.append(f"PE SELL x{times} (new pe_anchor={pe_anchor:.0f})")

            if use_rebalancing:
                new_ce = find_spot_for_target_delta(
                    positions, target_ce_delta, spot, dte
                )
                if new_ce is not None:
                    # Cap: CE anchor must stay below current spot so CEs don't
                    # trigger immediately.  Floor is current_spot − ce_gap.
                    capped_new_ce = min(new_ce, spot - CE_GAP)
                    old_ce = ce_anchor
                    if abs(capped_new_ce - spot) < abs(old_ce - spot):
                        ce_anchor = capped_new_ce
                        events.append(
                            f"  ↳ CE anchor rebalanced {old_ce:.0f} → {ce_anchor:.0f}"
                            + (f" (capped from {new_ce:.0f})" if new_ce > spot - CE_GAP else "")
                        )
                    else:
                        events.append(
                            f"  ↳ CE anchor kept at {old_ce:.0f} "
                            f"(computed {capped_new_ce:.0f} farther from spot {spot:.0f})"
                        )
                else:
                    events.append(
                        f"  ↳ CE anchor kept at {ce_anchor:.0f} (search out of range)"
                    )
                # Refresh both targets with updated portfolio and updated anchors
                target_ce_delta = compute_portfolio_delta(positions, ce_anchor, dte)
                target_pe_delta = compute_portfolio_delta(positions, pe_anchor, dte)

        # --- CE sell trigger: spot has fallen below ce_anchor - ce_gap ---
        if spot < ce_anchor and (ce_anchor - spot) > CE_GAP:
            times = int((ce_anchor - spot) / CE_GAP)
            ce_anchor -= CE_GAP * times
            for _ in range(times * LOTS_PER_SELL):
                sell_strike = round(spot + CE_OTM_DISTANCE, -2)
                positions.append(Position(strike=sell_strike, option_type="CE", quantity=-LOT_SIZE))
            events.append(f"CE SELL x{times} (new ce_anchor={ce_anchor:.0f})")

            if use_rebalancing:
                new_pe = find_spot_for_target_delta(
                    positions, target_pe_delta, spot, dte
                )
                if new_pe is not None:
                    # Cap: PE anchor must stay above current spot so PEs don't
                    # trigger immediately.  Ceiling is current_spot + pe_gap.
                    capped_new_pe = max(new_pe, spot + PE_GAP)
                    old_pe = pe_anchor
                    if abs(capped_new_pe - spot) < abs(old_pe - spot):
                        pe_anchor = capped_new_pe
                        events.append(
                            f"  ↳ PE anchor rebalanced {old_pe:.0f} → {pe_anchor:.0f}"
                            + (f" (capped from {new_pe:.0f})" if new_pe < spot + PE_GAP else "")
                        )
                    else:
                        events.append(
                            f"  ↳ PE anchor kept at {old_pe:.0f} "
                            f"(computed {capped_new_pe:.0f} farther from spot {spot:.0f})"
                        )
                else:
                    events.append(
                        f"  ↳ PE anchor kept at {pe_anchor:.0f} (search out of range)"
                    )
                target_ce_delta = compute_portfolio_delta(positions, ce_anchor, dte)
                target_pe_delta = compute_portfolio_delta(positions, pe_anchor, dte)

        net_delta_after = compute_portfolio_delta(positions, spot, dte)

        pe_lots = sum(abs(p.quantity) // LOT_SIZE for p in positions if p.option_type == "PE")
        ce_lots = sum(abs(p.quantity) // LOT_SIZE for p in positions if p.option_type == "CE")
        snapshot = {
            "tick": tick_idx,
            "spot": spot,
            "pe_anchor": pe_anchor,
            "ce_anchor": ce_anchor,
            "dte": dte,
            "net_delta": net_delta_after,
            "positions": len(positions),
            "pe_lots": pe_lots,
            "ce_lots": ce_lots,
            "events": events,
        }
        snapshots.append(snapshot)

        delta_change = f" Δdelta={net_delta_after - net_delta_before:+.1f}" if events else ""
        print(
            f"tick={tick_idx:02d}  spot={spot:7.0f}  PE_anch={pe_anchor:7.0f}  "
            f"CE_anch={ce_anchor:7.0f}  net_delta={net_delta_after:+7.2f}  "
            f"legs={len(positions)}{delta_change}"
        )
        for event_line in events:
            print(f"         {event_line}")

    print(f"\nFinal: {len(positions)} open legs, delta={compute_portfolio_delta(positions, PRICE_SERIES[-1], 0.1):+.2f}")
    return snapshots


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


def compare_simulations() -> None:
    """Run both modes and print a delta-at-reversal comparison."""
    snapshots_old = run_simulation(use_rebalancing=False)
    snapshots_new = run_simulation(use_rebalancing=True)

    print("\n" + "=" * 85)
    print("COMPARISON: net delta at each tick (OLD vs NEW)")
    print(
        f"{'Tick':>4}  {'Spot':>7}  {'Old delta':>10}  {'New delta':>10}  "
        f"{'Improvement':>12}  {'PE lots':>7}  {'Old CE':>7}  {'New CE':>7}"
    )
    print("-" * 85)
    for old, new in zip(snapshots_old, snapshots_new):
        improvement = new["net_delta"] - old["net_delta"]
        flag = " ★" if old["events"] or new["events"] else ""
        print(
            f"{old['tick']:>4}  {old['spot']:>7.0f}  {old['net_delta']:>+10.2f}  "
            f"{new['net_delta']:>+10.2f}  {improvement:>+12.2f}  "
            f"{old['pe_lots']:>7}  {old['ce_lots']:>7}  {new['ce_lots']:>7}{flag}"
        )
    print("\n★ = tick where a sell or rebalance occurred")
    print("PE lots: same in both runs (PE sells fire at identical ticks)")
    print("Old CE / New CE: diverge once rebalancing shifts the CE anchor higher")


if __name__ == "__main__":
    compare_simulations()
