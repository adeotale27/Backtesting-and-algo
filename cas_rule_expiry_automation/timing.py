"""CAS detect → sell execution timing records (live + backtest).

Captures:
  • cas_detected_at  — when CAS close first appears on the wire (3:28–3:30 window)
  • ce_sold_at       — when ATM+N Call market sell is submitted/acked
  • pe_sold_at       — when ATM−N Put market sell is submitted/acked
  • detect_to_*_ms   — wall-clock milliseconds from detect → each sell
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from cas_rule_expiry_automation.time_utils import get_ist_now


def _iso_now() -> str:
    return get_ist_now().isoformat(timespec="milliseconds")


@dataclass
class TimingEvent:
    """One expiry-day fire timeline for a single index."""

    index: str
    close_price: float
    trigger: str
    cas_detected_at: str
    ce_sold_at: Optional[str] = None
    pe_sold_at: Optional[str] = None
    ce_symbol: Optional[str] = None
    pe_symbol: Optional[str] = None
    ce_order_id: Any = None
    pe_order_id: Any = None
    detect_to_ce_ms: Optional[float] = None
    detect_to_pe_ms: Optional[float] = None
    detect_to_done_ms: Optional[float] = None
    dry_run: bool = True
    source: str = "live"  # live | backtest | manual
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def new_detect_event(
    index: str,
    close_price: float,
    trigger: str,
    source: str = "live",
    detected_at: Optional[str] = None,
) -> TimingEvent:
    return TimingEvent(
        index=index.upper(),
        close_price=float(close_price),
        trigger=trigger,
        cas_detected_at=detected_at or _iso_now(),
        source=source,
    )


def ms_between(start_iso: str, end_iso: str) -> float:
    """Milliseconds between two ISO timestamps (IST-aware)."""
    from datetime import datetime

    a = datetime.fromisoformat(start_iso)
    b = datetime.fromisoformat(end_iso)
    return (b - a).total_seconds() * 1000.0
