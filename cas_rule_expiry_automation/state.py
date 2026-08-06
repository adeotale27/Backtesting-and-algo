"""Runtime activation / fill state."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from cas_rule_expiry_automation.config import STATE_PATH
from cas_rule_expiry_automation.time_utils import get_ist_now


@dataclass
class Fill:
    ts: str
    index: str
    opt_type: str
    tradingsymbol: str
    strike: int
    quantity: int
    order_id: Any
    price: float
    dry_run: bool
    trigger: str
    close_price: float
    latency_ms: float = 0.0


@dataclass
class RuntimeState:
    activated: bool = False
    activated_at: Optional[str] = None
    ws_connected: bool = False
    fired_indexes: List[str] = field(default_factory=list)
    last_close: Dict[str, float] = field(default_factory=dict)
    last_ltp: Dict[str, float] = field(default_factory=dict)
    last_error: Optional[str] = None
    last_heartbeat: Optional[str] = None
    fills: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    ticks_seen: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StateStore:
    def __init__(self, path: str = STATE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._s = RuntimeState()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self._s = RuntimeState(**{
                k: raw.get(k, getattr(RuntimeState(), k))
                for k in RuntimeState.__dataclass_fields__
            })
            today = get_ist_now().date().isoformat()
            # Clear day-scoped fire state across midnight
            if self._s.fills and not str(self._s.fills[0].get("ts", "")).startswith(today):
                self._s.fired_indexes = []
                self._s.fills = []
                self._s.last_close = {}
        except Exception:
            self._s = RuntimeState()

    def _persist(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._s.to_dict(), fh, indent=2, default=str)
        os.replace(tmp, self.path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._s.to_dict()

    def activate(self, by: str = "admin") -> Dict[str, Any]:
        with self._lock:
            self._s.activated = True
            self._s.activated_at = get_ist_now().isoformat()
            self._s.last_error = None
            self._event("activated", f"by {by}")
            self._persist()
            return self._s.to_dict()

    def deactivate(self, by: str = "admin") -> Dict[str, Any]:
        with self._lock:
            self._s.activated = False
            self._event("deactivated", f"by {by}")
            self._persist()
            return self._s.to_dict()

    def is_activated(self) -> bool:
        with self._lock:
            return self._s.activated

    def has_fired(self, index: str) -> bool:
        with self._lock:
            return index.upper() in self._s.fired_indexes

    def mark_fired(self, index: str, close_price: float, fills: List[Fill]) -> None:
        with self._lock:
            idx = index.upper()
            if idx not in self._s.fired_indexes:
                self._s.fired_indexes.append(idx)
            self._s.last_close[idx] = close_price
            self._s.fills.extend(asdict(f) for f in fills)
            self._event("fired", f"{idx} close={close_price} legs={len(fills)}")
            self._persist()

    def set_ltp(self, index: str, ltp: float) -> None:
        with self._lock:
            self._s.last_ltp[index.upper()] = ltp

    def set_ws(self, connected: bool, ticks: int = 0) -> None:
        with self._lock:
            self._s.ws_connected = connected
            if ticks:
                self._s.ticks_seen = ticks
            self._s.last_heartbeat = get_ist_now().isoformat()
            self._persist()

    def set_error(self, msg: str) -> None:
        with self._lock:
            self._s.last_error = msg
            self._event("error", msg)
            self._persist()

    def reset_day(self) -> Dict[str, Any]:
        with self._lock:
            self._s.fired_indexes = []
            self._s.fills = []
            self._s.last_close = {}
            self._s.last_error = None
            self._event("reset", "day cleared")
            self._persist()
            return self._s.to_dict()

    def _event(self, kind: str, message: str) -> None:
        self._s.events.append(
            {"ts": get_ist_now().isoformat(), "kind": kind, "message": message}
        )
        if len(self._s.events) > 250:
            self._s.events = self._s.events[-250:]


_STORE: Optional[StateStore] = None
_LOCK = threading.Lock()


def get_store(path: str = STATE_PATH) -> StateStore:
    global _STORE
    with _LOCK:
        if _STORE is None or _STORE.path != path:
            _STORE = StateStore(path)
        return _STORE
