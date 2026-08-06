"""ATM / ATM±N strike resolution for NIFTY and SENSEX options."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

INDEX_META: Dict[str, Dict[str, Any]] = {
    "NIFTY": {
        "spot_key": "NSE:NIFTY 50",
        "exchange": "NFO",
        "segment": "NFO-OPT",
        "strike_gap": 50,
        "name": "NIFTY",
        "default_lot": 65,
        "token": 256265,
    },
    "SENSEX": {
        "spot_key": "BSE:SENSEX",
        "exchange": "BFO",
        "segment": "BFO-OPT",
        "strike_gap": 100,
        "name": "SENSEX",
        "default_lot": 20,
        "token": 265,
    },
}


@dataclass
class StrikeLeg:
    """Resolved option contract for one strategy leg."""

    index: str
    leg: str  # "CE" or "PE"
    strike: int
    tradingsymbol: str
    exchange: str
    instrument_token: int
    lot_size: int
    expiry: Optional[str] = None


def round_atm(spot: float, strike_gap: int) -> int:
    """Round spot to the nearest strike (exchange ATM convention)."""
    return int(round(spot / strike_gap) * strike_gap)


def target_strikes(
    close_price: float,
    strike_gap: int,
    ce_offset: int = 1,
    pe_offset: int = 1,
) -> Tuple[int, int, int]:
    """Return (atm, ce_strike, pe_strike) for ATM+ce_offset / ATM-pe_offset."""
    atm = round_atm(close_price, strike_gap)
    ce_strike = atm + ce_offset * strike_gap
    pe_strike = atm - pe_offset * strike_gap
    return atm, ce_strike, pe_strike


def _db_path() -> str:
    import os

    # Prefer parent repo instruments.db (shared with dashboard)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, "instruments.db")


def _query_option(
    index: str,
    expiry_prefix: str,
    strike: float,
    opt_type: str,
) -> Optional[Dict[str, Any]]:
    """Look up a CE/PE row from instruments.db by prefix + strike + type."""
    meta = INDEX_META[index]
    path = _db_path()
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.error("Cannot open instruments.db: %s", exc)
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM instruments
            WHERE tradingsymbol LIKE ?
              AND instrument_type = ?
              AND segment = ?
              AND ABS(strike - ?) < 0.01
            LIMIT 1
            """,
            (f"{expiry_prefix}%", opt_type, meta["segment"], float(strike)),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def detect_expiry_prefix(kite: Any, index: str, on_date: Optional[date] = None) -> Optional[str]:
    """Find today's (or ``on_date``) option symbol prefix for the index.

    Mirrors ``ExpiryTradeSystem.detect_expiry`` / common-prefix extraction.
    """
    meta = INDEX_META[index]
    target = on_date or date.today()
    try:
        instruments = kite.instruments(meta["exchange"])
    except Exception as exc:
        logger.error("instruments(%s) failed: %s", meta["exchange"], exc)
        return None

    symbols = [
        inst["tradingsymbol"]
        for inst in instruments
        if inst["tradingsymbol"].startswith(meta["name"])
        and inst.get("expiry") == target
        and inst.get("instrument_type") in ("CE", "PE")
    ]
    if not symbols:
        return None
    return _common_prefix(symbols)


def has_expiry_today(kite: Any, index: str) -> bool:
    return detect_expiry_prefix(kite, index) is not None


def _common_prefix(symbols: List[str]) -> str:
    if not symbols:
        return ""
    prefix = symbols[0]
    for sym in symbols[1:]:
        while not sym.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def resolve_legs(
    kite: Any,
    index: str,
    close_price: float,
    ce_offset: int = 1,
    pe_offset: int = 1,
    expiry_prefix: Optional[str] = None,
) -> List[StrikeLeg]:
    """Resolve ATM±N CE/PE instruments for ``close_price``."""
    meta = INDEX_META[index]
    gap = int(meta["strike_gap"])
    atm, ce_strike, pe_strike = target_strikes(close_price, gap, ce_offset, pe_offset)

    prefix = expiry_prefix or detect_expiry_prefix(kite, index)
    if not prefix:
        raise RuntimeError(f"No {index} expiry contracts found for today")

    legs: List[StrikeLeg] = []
    for leg_name, strike in (("CE", ce_strike), ("PE", pe_strike)):
        row = _query_option(index, prefix, strike, leg_name)
        if row is None:
            # Fallback: scan live instruments dump
            row = _find_from_kite(kite, meta, prefix, strike, leg_name)
        if row is None:
            raise RuntimeError(
                f"Could not resolve {index} {leg_name} strike={strike} prefix={prefix}"
            )
        lot = int(row.get("lot_size") or meta["default_lot"])
        legs.append(
            StrikeLeg(
                index=index,
                leg=leg_name,
                strike=int(row.get("strike") or strike),
                tradingsymbol=row["tradingsymbol"],
                exchange=meta["exchange"],
                instrument_token=int(row.get("instrument_token") or 0),
                lot_size=lot,
                expiry=str(row.get("expiry") or ""),
            )
        )
    logger.info(
        "Resolved %s ATM=%s close=%.2f → CE %s / PE %s",
        index,
        atm,
        close_price,
        legs[0].tradingsymbol,
        legs[1].tradingsymbol,
    )
    return legs


def _find_from_kite(
    kite: Any,
    meta: Dict[str, Any],
    prefix: str,
    strike: float,
    opt_type: str,
) -> Optional[Dict[str, Any]]:
    try:
        instruments = kite.instruments(meta["exchange"])
    except Exception:
        return None
    for inst in instruments:
        if (
            inst["tradingsymbol"].startswith(prefix)
            and inst.get("instrument_type") == opt_type
            and abs(float(inst.get("strike") or 0) - float(strike)) < 0.01
        ):
            return inst
    return None


def get_lot_size(index: str) -> int:
    """Best-effort lot size from parent instrument_cache, else defaults."""
    meta = INDEX_META[index]
    try:
        import instrument_cache

        if index == "NIFTY":
            return int(instrument_cache.get_lot_size("NIFTY 50") or meta["default_lot"])
        if index == "SENSEX":
            return int(instrument_cache.get_sensex_lot_size() or meta["default_lot"])
    except Exception:
        pass
    return int(meta["default_lot"])
