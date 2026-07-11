"""Tests for the wave extractor duplicate-instance guard.

Covers the NIFTY2670724400CE incident: two scraper processes for one symbol
each placed their own BUY+SELL duo (doubled legs) because /api/start had no
duplicate check and both processes overwrote the same status file.

Real processes and real files are used throughout (no mocked DB layer, per
project convention). Only subprocess.Popen inside flask_app is patched where a
test must not launch a real order-placing scraper.
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flask_app
from flask_app import app

TEST_SYMBOL = "TESTWEGUARD24400CE"

# Captured before any monkeypatching so fixtures can always spawn for real
_REAL_POPEN = subprocess.Popen


def _spawn_fake_scraper(tmp_path, symbol: str) -> subprocess.Popen:
    """Launch a real process whose cmdline matches a live scraper's.

    The process table entry looks like:
        python .../ticker_single_scraper_new.py 10 10 <symbol> 325:325 tok
    so _find_running_scrapers() detects it exactly like a real instance.
    """
    fake_script = tmp_path / "ticker_single_scraper_new.py"
    fake_script.write_text("import time\ntime.sleep(120)\n")
    return _REAL_POPEN(
        [sys.executable, str(fake_script), "10", "10", symbol, "325:325", "tok"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def fake_scraper(tmp_path):
    """A live fake scraper process for TEST_SYMBOL; killed on teardown."""
    proc = _spawn_fake_scraper(tmp_path, TEST_SYMBOL)
    time.sleep(0.3)  # let it appear in the process table
    yield proc
    proc.kill()
    proc.wait()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Authenticated Flask client with LOG_DIR redirected to tmp."""
    monkeypatch.setattr(flask_app, "LOG_DIR", str(tmp_path / "logs"))
    os.makedirs(str(tmp_path / "logs"), exist_ok=True)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["app_authenticated"] = True
        yield test_client


def _start_payload(symbol: str = TEST_SYMBOL, force: bool = False) -> dict:
    return {
        "symbol": symbol,
        "buy_gap": 10,
        "sell_gap": 10,
        "buy_quantity": 325,
        "sell_quantity": 325,
        "request_token": "access:dummy-test-token",
        "force": force,
    }


class _DummyProcess:
    """Stands in for Popen when a test must not launch a real scraper."""

    pid = 999999


def _patch_scraper_spawn(monkeypatch, on_scraper_spawn):
    """Patch subprocess.Popen so scraper spawns hit ``on_scraper_spawn`` while
    every other command (e.g. the ``ps`` scan inside _find_running_scrapers)
    passes through to the real Popen."""
    real_popen = subprocess.Popen

    def _dispatch(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if any("ticker_single_scraper" in str(part) for part in cmd):
            return on_scraper_spawn(*args, **kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(flask_app.subprocess, "Popen", _dispatch)


class TestFindRunningScrapers:
    def test_sees_live_process(self, fake_scraper):
        found = flask_app._find_running_scrapers(TEST_SYMBOL)
        assert [p["pid"] for p in found] == [fake_scraper.pid]
        assert found[0]["symbol"] == TEST_SYMBOL
        assert found[0]["started"]

    def test_filters_by_symbol(self, fake_scraper):
        assert flask_app._find_running_scrapers("SOMEOTHERSYMBOL") == []

    def test_dead_process_not_found(self, tmp_path):
        proc = _spawn_fake_scraper(tmp_path, TEST_SYMBOL)
        proc.kill()
        proc.wait()
        time.sleep(0.2)
        assert flask_app._find_running_scrapers(TEST_SYMBOL) == []


class TestStartGuard:
    def test_409_on_duplicate(self, client, fake_scraper, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("Popen must not spawn a scraper when duplicate exists")

        _patch_scraper_spawn(monkeypatch, _fail)
        response = client.post("/api/start", json=_start_payload())
        assert response.status_code == 409
        body = response.get_json()
        assert body["error"] == "already_running"
        assert body["pid"] == fake_scraper.pid
        assert body["symbol"] == TEST_SYMBOL

    def test_200_when_no_instance(self, client, monkeypatch):
        _patch_scraper_spawn(monkeypatch, lambda *a, **k: _DummyProcess())
        response = client.post("/api/start", json=_start_payload())
        assert response.status_code == 200
        assert response.get_json()["pid"] == _DummyProcess.pid

    def test_force_bypasses_duplicate(self, client, fake_scraper, monkeypatch):
        _patch_scraper_spawn(monkeypatch, lambda *a, **k: _DummyProcess())
        response = client.post("/api/start", json=_start_payload(force=True))
        assert response.status_code == 200

    def test_log_file_appended_not_truncated(self, client, monkeypatch):
        _patch_scraper_spawn(monkeypatch, lambda *a, **k: _DummyProcess())
        log_path = os.path.join(flask_app.LOG_DIR, f"{TEST_SYMBOL}.log")
        with open(log_path, "w") as f:
            f.write("previous run history\n")
        response = client.post("/api/start", json=_start_payload())
        assert response.status_code == 200
        with open(log_path) as f:
            content = f.read()
        assert "previous run history" in content
        assert "=== scraper start" in content

    def test_concurrent_double_request_single_spawn(self, tmp_path, monkeypatch):
        """Two simultaneous starts for one symbol → exactly one process.

        Popen is patched to spawn a REAL fake scraper (matching cmdline), so
        after the first request spawns, the second request's in-lock check
        sees a live process and must 409.
        """
        spawned: list = []

        def _spawn_real(*args, **kwargs):
            proc = _spawn_fake_scraper(tmp_path, TEST_SYMBOL)
            spawned.append(proc)
            time.sleep(0.3)  # ensure it's visible in the process table
            return proc

        _patch_scraper_spawn(monkeypatch, _spawn_real)
        monkeypatch.setattr(flask_app, "LOG_DIR", str(tmp_path / "logs"))
        os.makedirs(str(tmp_path / "logs"), exist_ok=True)
        app.config["TESTING"] = True

        status_codes: list = []

        def _post_start() -> None:
            with app.test_client() as test_client:
                with test_client.session_transaction() as sess:
                    sess["app_authenticated"] = True
                response = test_client.post("/api/start", json=_start_payload())
                status_codes.append(response.status_code)

        threads = [threading.Thread(target=_post_start) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert sorted(status_codes) == [200, 409]
            assert len(spawned) == 1
        finally:
            for proc in spawned:
                proc.kill()
                proc.wait()


class TestStatusDuplicateFlag:
    def test_two_live_pids_flagged_duplicate(self, client, tmp_path, monkeypatch):
        """Two per-PID status files with live PIDs → both flagged duplicate."""
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        monkeypatch.setattr(flask_app, "STATUS_DIR", str(status_dir))

        live_a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        live_b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            for pid in (live_a.pid, live_b.pid):
                path = status_dir / f"status_{TEST_SYMBOL}_{pid}.json"
                path.write_text(json.dumps({
                    "symbol": TEST_SYMBOL, "pid": pid, "orders": {},
                    "buy_gap": 10, "sell_gap": 10, "quantity": "325",
                }))
            # An unrelated single instance must NOT be flagged
            single = status_dir / f"status_OTHERSYM_{live_a.pid}.json"
            single.write_text(json.dumps({
                "symbol": "OTHERSYM", "pid": live_a.pid, "orders": {},
            }))

            response = client.get("/api/status")
            assert response.status_code == 200
            by_symbol: dict = {}
            for entry in response.get_json():
                by_symbol.setdefault(entry["symbol"], []).append(entry)

            duplicates = by_symbol[TEST_SYMBOL]
            assert len(duplicates) == 2
            assert all(entry["duplicate"] for entry in duplicates)
            assert all(entry["is_running"] for entry in duplicates)
            assert not by_symbol["OTHERSYM"][0]["duplicate"]
        finally:
            for proc in (live_a, live_b):
                proc.kill()
                proc.wait()

    def test_dead_pid_not_flagged(self, client, tmp_path, monkeypatch):
        """A stale status file (dead PID) is not running and not duplicate."""
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        monkeypatch.setattr(flask_app, "STATUS_DIR", str(status_dir))

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path = status_dir / f"status_{TEST_SYMBOL}_{dead.pid}.json"
        path.write_text(json.dumps({
            "symbol": TEST_SYMBOL, "pid": dead.pid, "orders": {},
        }))

        response = client.get("/api/status")
        entries = [e for e in response.get_json() if e["symbol"] == TEST_SYMBOL]
        assert len(entries) == 1
        assert not entries[0]["is_running"]
        assert not entries[0]["duplicate"]
