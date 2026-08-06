"""Runtime activation / fill / timing state."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from cas_rule_expiry_automation.config import STATE_PATH
from cas_rule_expiry_automation.time_utils import get_ist_now
from cas_rule_expiry_automation.timing import TimingEvent


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
    cas_detected_at: Optional[str] = None
    order_submitted_at: Optional[str] = None


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
    timings: List[Dict[str, Any]] = field(default_factory=list)
    ticks_seen: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StateStore:
    def __init__(self, path: str = STATE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._s = RuntimeState()
        self._day_checked_at: float = 0.0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            defaults = RuntimeState()
            kwargs = {}
            for k in RuntimeState.__dataclass_fields__:
                kwargs[k] = raw.get(k, getattr(defaults, k))
            self._s = RuntimeState(**kwargs)
            today = get_ist_now().date().isoformat()
            if self._s.activated and self._s.activated_at and not str(self._s.activated_at).startswith(today):
                self._s.activated = False
                self._s.activated_at = None
            if self._s.fills and not str(self._s.fills[0].get("ts", "")).startswith(today):
                self._s.fired_indexes = []
                self._s.fills = []
                self._s.last_close = {}
                self._s.timings = []
        except Exception:
            self._s = RuntimeState()

    def _persist(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._s.to_dict(), fh, indent=2, default=str)
        os.replace(tmp, self.path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_same_day()
            return self._s.to_dict()

    def activate(self, by: str = "admin") -> Dict[str, Any]:
        with self._lock:
            self._ensure_same_day()
            if self._s.activated:
                # Idempotent — already active; do not spam events
                return {**self._s.to_dict(), "unchanged": True}
            self._s.activated = True
            self._s.activated_at = get_ist_now().isoformat()
            self._s.last_error = None
            self._event("activated", f"CAS window activated by {by}")
            self._persist()
            return {**self._s.to_dict(), "unchanged": False}

    def deactivate(self, by: str = "admin") -> Dict[str, Any]:
        with self._lock:
            self._ensure_same_day()
            if not self._s.activated:
                return {**self._s.to_dict(), "unchanged": True}
            self._s.activated = False
            self._event("deactivated", f"CAS window deactivated by {by}")
            self._persist()
            return {**self._s.to_dict(), "unchanged": False}

    def is_activated(self) -> bool:
        # Fast path for WS ticks — avoid day-rollover IO on every message.
        with self._lock:
            if self._day_checked_at == 0.0 or (time.monotonic() - self._day_checked_at) > 30.0:
                self._ensure_same_day()
                self._day_checked_at = time.monotonic()
            return self._s.activated

    def _ensure_same_day(self) -> None:
        """Drop yesterday's arm state so a leftover ACTIVATED does not fire next day."""
        today = get_ist_now().date().isoformat()
        at = self._s.activated_at or ""
        if self._s.activated and at and not str(at).startswith(today):
            self._s.activated = False
            self._s.activated_at = None
            self._event("auto_deactivated", "new IST day — CAS window reset")
            self._persist()
        # Also clear stale fills/fires if from a prior day
        if self._s.fills and not str(self._s.fills[0].get("ts", "")).startswith(today):
            self._s.fired_indexes = []
            self._s.fills = []
            self._s.last_close = {}
            self._s.timings = []
            self._persist()

    def has_fired(self, index: str) -> bool:
        # Lock-free read of the in-memory fired set (updated under lock elsewhere).
        return index.upper() in self._s.fired_indexes

    def mark_fired(
        self,
        index: str,
        close_price: float,
        fills: List[Fill],
        timing: Optional[TimingEvent] = None,
    ) -> None:
        with self._lock:
            idx = index.upper()
            if idx not in self._s.fired_indexes:
                self._s.fired_indexes.append(idx)
            self._s.last_close[idx] = close_price
            self._s.fills.extend(asdict(f) for f in fills)
            if timing is not None:
                self._s.timings.append(timing.to_dict())
                msg = (
                    f"{idx} CAS@{timing.cas_detected_at} → "
                    f"CE@{timing.ce_sold_at or '—'} ({timing.detect_to_ce_ms}ms) "
                    f"PE@{timing.pe_sold_at or '—'} ({timing.detect_to_pe_ms}ms) "
                    f"total={timing.detect_to_done_ms}ms"
                )
            else:
                msg = f"{idx} close={close_price} legs={len(fills)}"
            self._event("fired", msg)
            self._persist()

    def add_timing(self, timing: TimingEvent) -> None:
        with self._lock:
            self._s.timings.append(timing.to_dict())
            self._persist()

    def set_ltp(self, index: str, ltp: float) -> None:
        # Hot path — memory only; never block WS on disk IO.
        with self._lock:
            self._s.last_ltp[index.upper()] = ltp

    def set_ws(self, connected: bool, ticks: int = 0) -> None:
        # Heartbeat is ephemeral — do NOT persist (was blocking fire path every ~50ms).
        with self._lock:
            self._s.ws_connected = connected
            if ticks:
                self._s.ticks_seen = ticks
            self._s.last_heartbeat = get_ist_now().isoformat()

    def set_error(self, msg: str) -> None:
        with self._lock:
            self._s.last_error = msg
            self._event("error", msg)
            self._persist()

    def reset_day(self) -> Dict[str, Any]:
        with self._lock:
            self._s.fired_indexes = []
            self._s.fills = []
            self._s.timings = []
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
