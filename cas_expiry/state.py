"""Runtime activation state for the CAS Expiry algo.

Persisted to ``runtime_state.json`` so an admin activation survives
process restarts within the same trading day. Activation is always
manual — nothing auto-arms on boot.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from cas_expiry.config import STATE_PATH
from cas_expiry.time_utils import get_ist_now


@dataclass
class FillRecord:
    """One leg fill (or dry-run fill) produced by the strategy."""

    ts: str
    index: str
    leg: str
    tradingsymbol: str
    strike: int
    side: str
    quantity: int
    order_id: Any
    price: float
    dry_run: bool
    note: str = ""


@dataclass
class RuntimeState:
    """Mutable, process-shared strategy state."""

    activated: bool = False
    activated_at: Optional[str] = None
    activated_by: Optional[str] = None
    fired_today: bool = False
    fired_indexes: List[str] = field(default_factory=list)
    fired_at: Optional[str] = None
    last_close_price: Optional[float] = None
    last_error: Optional[str] = None
    last_heartbeat: Optional[str] = None
    fills: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StateStore:
    """Thread-safe JSON-backed activation / fill store."""

    def __init__(self, path: str = STATE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._state = RuntimeState()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self._state = RuntimeState(
                activated=bool(raw.get("activated", False)),
                activated_at=raw.get("activated_at"),
                activated_by=raw.get("activated_by"),
                fired_today=bool(raw.get("fired_today", False)),
                fired_indexes=list(raw.get("fired_indexes") or []),
                fired_at=raw.get("fired_at"),
                last_close_price=raw.get("last_close_price"),
                last_error=raw.get("last_error"),
                last_heartbeat=raw.get("last_heartbeat"),
                fills=list(raw.get("fills") or []),
                events=list(raw.get("events") or []),
            )
            today = get_ist_now().date().isoformat()
            if self._state.fired_at and not str(self._state.fired_at).startswith(today):
                self._state.fired_today = False
                self._state.fired_indexes = []
                self._state.fired_at = None
                self._state.fills = []
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._state = RuntimeState()

    def _persist(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state.to_dict(), fh, indent=2, default=str)
        os.replace(tmp, self.path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._state.to_dict()

    def activate(self, by: str = "admin") -> Dict[str, Any]:
        with self._lock:
            now = get_ist_now().isoformat()
            self._state.activated = True
            self._state.activated_at = now
            self._state.activated_by = by
            self._state.last_error = None
            self._append_event_unlocked("activated", f"Activated by {by}")
            self._persist()
            return self._state.to_dict()

    def deactivate(self, by: str = "admin") -> Dict[str, Any]:
        with self._lock:
            self._state.activated = False
            self._append_event_unlocked("deactivated", f"Deactivated by {by}")
            self._persist()
            return self._state.to_dict()

    def is_activated(self) -> bool:
        with self._lock:
            return bool(self._state.activated)

    def has_fired_today(self) -> bool:
        with self._lock:
            return bool(self._state.fired_today)

    def has_fired_index(self, index: str) -> bool:
        with self._lock:
            return index.upper() in self._state.fired_indexes

    def mark_fired(
        self,
        close_price: float,
        fills: List[FillRecord],
        index: Optional[str] = None,
    ) -> None:
        with self._lock:
            now = get_ist_now().isoformat()
            self._state.fired_today = True
            self._state.fired_at = now
            self._state.last_close_price = close_price
            idx = (index or (fills[0].index if fills else "")).upper()
            if idx and idx not in self._state.fired_indexes:
                self._state.fired_indexes.append(idx)
            # Append fills so BOTH indexes can accumulate legs the same day
            self._state.fills.extend(asdict(f) for f in fills)
            self._append_event_unlocked(
                "fired",
                f"{idx or '?'} CAS close={close_price} → {len(fills)} leg(s)",
            )
            self._persist()

    def set_error(self, message: str) -> None:
        with self._lock:
            self._state.last_error = message
            self._append_event_unlocked("error", message)
            self._persist()

    def heartbeat(self) -> None:
        with self._lock:
            self._state.last_heartbeat = get_ist_now().isoformat()
            self._persist()

    def reset_day(self) -> Dict[str, Any]:
        """Clear fired/fills for re-testing (does not change activation)."""
        with self._lock:
            self._state.fired_today = False
            self._state.fired_indexes = []
            self._state.fired_at = None
            self._state.fills = []
            self._state.last_close_price = None
            self._state.last_error = None
            self._append_event_unlocked("reset", "Day state cleared")
            self._persist()
            return self._state.to_dict()

    def _append_event_unlocked(self, kind: str, message: str) -> None:
        self._state.events.append(
            {
                "ts": get_ist_now().isoformat(),
                "kind": kind,
                "message": message,
            }
        )
        if len(self._state.events) > 200:
            self._state.events = self._state.events[-200:]


_STORE: Optional[StateStore] = None
_STORE_LOCK = threading.Lock()


def get_store(path: str = STATE_PATH) -> StateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None or _STORE.path != path:
            _STORE = StateStore(path)
        return _STORE
