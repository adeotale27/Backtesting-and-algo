"""Background runner — watches activation flag and CAS window."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from cas_expiry.config import CasConfig, load_config
from cas_expiry.kite_session import KiteSession
from cas_expiry.state import StateStore, get_store
from cas_expiry.strategy import CasExpiryStrategy
from cas_expiry.time_utils import get_ist_now, in_window

logger = logging.getLogger(__name__)


class CasRunner:
    """Daemon thread that arms the strategy when admin-activated."""

    def __init__(
        self,
        config: Optional[CasConfig] = None,
        store: Optional[StateStore] = None,
    ) -> None:
        self.config = config or load_config()
        self.store = store or get_store()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._session: Optional[KiteSession] = None
        self._strategy: Optional[CasExpiryStrategy] = None
        self._watch_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cas-expiry-runner", daemon=True
        )
        self._thread.start()
        logger.info("CAS runner started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("CAS runner stopped")

    def _ensure_strategy(self) -> CasExpiryStrategy:
        with self._watch_lock:
            if self._strategy is None:
                self.config = load_config(self.config.config_path)
                self._session = KiteSession(self.config)
                self._session.connect()
                self._session.sync_instruments()
                self._strategy = CasExpiryStrategy(
                    self._session, self.config, self.store
                )
            return self._strategy

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.heartbeat()
                if not self.store.is_activated():
                    time.sleep(self.config.idle_poll_seconds)
                    continue

                now = get_ist_now()
                approaching = in_window(
                    now,
                    _shift(self.config.watch_start, -10),
                    self.config.watch_end,
                )
                if not approaching:
                    time.sleep(self.config.idle_poll_seconds)
                    continue

                pending = [
                    idx
                    for idx in self.config.indexes()
                    if not self.store.has_fired_index(idx)
                ]
                if not pending:
                    time.sleep(self.config.idle_poll_seconds)
                    continue

                strategy = self._ensure_strategy()
                self._watch_indexes(strategy, pending)
            except Exception as exc:
                logger.exception("Runner loop error: %s", exc)
                self.store.set_error(str(exc))
                time.sleep(2)

    def _watch_indexes(self, strategy: CasExpiryStrategy, indexes: List[str]) -> None:
        """Watch one or more indexes in parallel during the CAS window."""

        def _run_one(index: str) -> None:
            if self._stop.is_set() or not self.store.is_activated():
                return
            if self.store.has_fired_index(index):
                return
            logger.info("Entering CAS watch for %s", index)
            try:
                strategy.run_index(
                    index,
                    stop_flag=lambda: self._stop.is_set()
                    or not self.store.is_activated(),
                )
            except Exception as exc:
                logger.exception("CAS run failed for %s: %s", index, exc)
                self.store.set_error(f"{index}: {exc}")

        if len(indexes) == 1:
            _run_one(indexes[0])
            return

        with ThreadPoolExecutor(max_workers=len(indexes), thread_name_prefix="cas") as pool:
            futures = [pool.submit(_run_one, idx) for idx in indexes]
            for fut in as_completed(futures):
                # Surface unexpected thread errors
                fut.result()


def _shift(t, minutes: int):
    """Shift a datetime.time by ``minutes`` (can be negative)."""
    from datetime import datetime, timedelta

    base = datetime(2000, 1, 1, t.hour, t.minute, t.second)
    shifted = base + timedelta(minutes=minutes)
    return shifted.time()


_RUNNER: Optional[CasRunner] = None
_RUNNER_LOCK = threading.Lock()


def get_runner() -> CasRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = CasRunner()
        return _RUNNER
