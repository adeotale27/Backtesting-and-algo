"""Parity and behavior tests for greeks_lib (mibian vs opengreeks backends).

The parity grid requires the opengreeks package; those tests are skipped
where the wheel is unavailable. Config/fail-loud tests always run.
"""

import logging
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import greeks_lib
import mibian

OPENGREEKS_AVAILABLE = True
try:
    import opengreeks  # noqa: F401
except ImportError:
    OPENGREEKS_AVAILABLE = False

INTEREST_RATE_PCT = 10.0


@pytest.fixture(autouse=True)
def _restore_backend():
    """Reset greeks_lib module state after each test."""
    yield
    greeks_lib._backend = "mibian"
    greeks_lib._shadow_compare = False
    greeks_lib._shadow_tolerance = 0.005
    greeks_lib._shadow_last_log.clear()
    greeks_lib._shadow_suppressed.clear()


def _use_backend(library: str, shadow: bool = False) -> None:
    """Force a backend without touching configfile.ini."""
    if library == "opengreeks" or shadow:
        greeks_lib._opengreeks_bs = greeks_lib._import_opengreeks()
    greeks_lib._backend = library
    greeks_lib._shadow_compare = shadow


def _write_config(tmp_path, body: str) -> str:
    config_file = tmp_path / "configfile.ini"
    config_file.write_text(body)
    return str(config_file)


# ---------------------------------------------------------------------------
# Config / fail-loud behavior (always run)
# ---------------------------------------------------------------------------

class TestConfig:
    def test_missing_section_defaults_to_mibian(self, tmp_path):
        library, shadow, tolerance = greeks_lib._load_config(
            _write_config(tmp_path, "[others]\nfoo = bar\n")
        )
        assert (library, shadow, tolerance) == ("mibian", False, 0.005)

    def test_valid_opengreeks_selection(self, tmp_path):
        library, shadow, tolerance = greeks_lib._load_config(
            _write_config(
                tmp_path,
                "[greeks]\nlibrary = opengreeks\nshadow_compare = true\n"
                "shadow_tolerance = 0.01\n",
            )
        )
        assert (library, shadow, tolerance) == ("opengreeks", True, 0.01)

    def test_invalid_library_raises(self, tmp_path):
        with pytest.raises(greeks_lib.GreeksBackendError, match="must be one of"):
            greeks_lib._load_config(
                _write_config(tmp_path, "[greeks]\nlibrary = vollib\n")
            )

    def test_missing_opengreeks_package_raises(self, tmp_path, monkeypatch):
        def _raise_import(*_args, **_kwargs):
            raise greeks_lib.GreeksBackendError(
                "requires opengreeks but it is not installed"
            )

        monkeypatch.setattr(greeks_lib, "_import_opengreeks", _raise_import)
        monkeypatch.setattr(greeks_lib, "_opengreeks_bs", None)
        with pytest.raises(greeks_lib.GreeksBackendError, match="not installed"):
            greeks_lib.reread_greeks_config(
                _write_config(tmp_path, "[greeks]\nlibrary = opengreeks\n")
            )

    def test_shadow_import_failure_degrades_instead_of_crashing(
        self, tmp_path, monkeypatch, caplog
    ):
        """A broken shadow backend must disable shadow, not kill the app.

        Regression test for the 2026-07-08 prod incident: opengreeks wheels
        are unusable on Python 3.11 (undefined symbol), and library=mibian +
        shadow_compare=true put flask-trading into a crash-restart loop.
        """
        def _raise_import(*_args, **_kwargs):
            raise greeks_lib.GreeksBackendError(
                "opengreeks is installed but failed to import on this "
                "interpreter: undefined symbol: PyErr_SetRaisedException"
            )

        monkeypatch.setattr(greeks_lib, "_import_opengreeks", _raise_import)
        monkeypatch.setattr(greeks_lib, "_opengreeks_bs", None)
        with caplog.at_level(logging.CRITICAL, logger="greeks_lib"):
            greeks_lib.reread_greeks_config(
                _write_config(
                    tmp_path,
                    "[greeks]\nlibrary = mibian\nshadow_compare = true\n",
                )
            )
        assert greeks_lib.get_active_backend() == "mibian"
        assert greeks_lib._shadow_compare is False
        critical_logs = [
            r for r in caplog.records if "shadow comparison DISABLED" in r.message
        ]
        assert len(critical_logs) == 1
        # And BS still works on the primary backend.
        assert greeks_lib.BS([24000, 24200, 10, 30], volatility=15).callDelta

    def test_broken_install_message_names_real_error(self, monkeypatch):
        """Installed-but-broken opengreeks must not report 'not installed'."""
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "opengreeks":
                raise ImportError("undefined symbol: PyErr_SetRaisedException")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        with pytest.raises(
            greeks_lib.GreeksBackendError, match="failed to import"
        ):
            greeks_lib._import_opengreeks()

    def test_reread_switches_backend(self, tmp_path):
        greeks_lib.reread_greeks_config(
            _write_config(tmp_path, "[greeks]\nlibrary = mibian\n")
        )
        assert greeks_lib.get_active_backend() == "mibian"


# ---------------------------------------------------------------------------
# mibian passthrough semantics (always run)
# ---------------------------------------------------------------------------

class TestMibianPassthrough:
    def test_values_identical_to_raw_mibian(self):
        _use_backend("mibian")
        args = [24000.0, 24200.0, INTEREST_RATE_PCT, 30.0]
        wrapped = greeks_lib.BS(args, volatility=15.0)
        raw = mibian.BS(args, volatility=15.0)
        for attribute in ("callPrice", "putPrice", "callDelta", "putDelta",
                          "callTheta", "putTheta", "vega", "gamma"):
            assert getattr(wrapped, attribute) == getattr(raw, attribute)

    def test_iv_backsolve_identical(self):
        _use_backend("mibian")
        args = [24000.0, 24200.0, INTEREST_RATE_PCT, 30.0]
        wrapped = greeks_lib.BS(args, callPrice=410.77)
        raw = mibian.BS(args, callPrice=410.77)
        assert wrapped.impliedVolatility == raw.impliedVolatility

    def test_zero_volatility_leaves_attrs_none(self):
        _use_backend("mibian")
        bs = greeks_lib.BS([24000, 24200, 10, 30], volatility=0)
        assert bs.callDelta is None
        assert bs.callPrice is None


# ---------------------------------------------------------------------------
# Parity grid (needs opengreeks)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not OPENGREEKS_AVAILABLE, reason="opengreeks not installed")
class TestParityGrid:
    @pytest.mark.parametrize("underlying", [24000.0, 81000.0])
    @pytest.mark.parametrize("moneyness", [0.85, 0.95, 1.0, 1.05, 1.15])
    @pytest.mark.parametrize("days", [0.5, 1.0, 5.0, 30.0])
    @pytest.mark.parametrize("volatility_pct", [8.0, 15.0, 40.0, 90.0])
    def test_greeks_match(self, underlying, moneyness, days, volatility_pct):
        strike = round(underlying * moneyness)
        args = [underlying, strike, INTEREST_RATE_PCT, days]
        _use_backend("mibian")
        reference = greeks_lib.BS(args, volatility=volatility_pct)
        _use_backend("opengreeks")
        candidate = greeks_lib.BS(args, volatility=volatility_pct)
        assert candidate.callPrice == pytest.approx(
            reference.callPrice, rel=1e-4, abs=1e-6
        )
        assert candidate.putPrice == pytest.approx(
            reference.putPrice, rel=1e-4, abs=1e-6
        )
        assert candidate.callDelta == pytest.approx(reference.callDelta, abs=1e-4)
        assert candidate.putDelta == pytest.approx(reference.putDelta, abs=1e-4)
        assert candidate.callTheta == pytest.approx(reference.callTheta, abs=1e-3)
        assert candidate.putTheta == pytest.approx(reference.putTheta, abs=1e-3)

    @pytest.mark.parametrize("option_type", ["call", "put"])
    @pytest.mark.parametrize("volatility_pct", [10.0, 20.0, 60.0])
    def test_iv_round_trip(self, option_type, volatility_pct):
        args = [24000.0, 24200.0, INTEREST_RATE_PCT, 15.0]
        priced = mibian.BS(args, volatility=volatility_pct)
        price_kwargs = (
            {"callPrice": priced.callPrice}
            if option_type == "call"
            else {"putPrice": priced.putPrice}
        )
        _use_backend("opengreeks")
        solved = greeks_lib.BS(args, **price_kwargs).impliedVolatility
        assert solved == pytest.approx(volatility_pct, abs=0.25)


@pytest.mark.skipif(not OPENGREEKS_AVAILABLE, reason="opengreeks not installed")
class TestOpengreeksEdgeCases:
    def test_below_intrinsic_price_returns_zero_iv(self):
        _use_backend("opengreeks")
        bs = greeks_lib.BS([24000, 20000, 10, 5], callPrice=100.0)
        assert bs.impliedVolatility == 0.0

    def test_zero_volatility_leaves_attrs_none(self):
        _use_backend("opengreeks")
        bs = greeks_lib.BS([24000, 24200, 10, 30], volatility=0)
        assert bs.callDelta is None

    def test_zero_strike_raises_like_mibian(self):
        _use_backend("opengreeks")
        with pytest.raises(ZeroDivisionError):
            greeks_lib.BS([24000, 0, 10, 30], volatility=15)

    def test_zero_dte_does_not_crash(self):
        _use_backend("opengreeks")
        bs = greeks_lib.BS([24000, 24200, 10, 0], volatility=15)
        assert bs.callDelta is not None
        assert math.isfinite(bs.callDelta)


# ---------------------------------------------------------------------------
# Shadow compare (needs opengreeks)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not OPENGREEKS_AVAILABLE, reason="opengreeks not installed")
class TestShadowCompare:
    def test_no_warning_when_backends_agree(self, caplog):
        _use_backend("mibian", shadow=True)
        with caplog.at_level(logging.WARNING, logger="greeks_lib"):
            greeks_lib.BS([24000, 24200, 10, 30], volatility=15)
        assert not [r for r in caplog.records if "divergence" in r.message]

    def test_divergence_logged_and_rate_limited(self, caplog, monkeypatch):
        _use_backend("mibian", shadow=True)

        def _skewed(args, volatility, callPrice, putPrice):
            values = greeks_lib._compute_mibian(
                args, volatility, callPrice, putPrice
            )
            if values["callDelta"] is not None:
                values["callDelta"] += 0.5
            return values

        monkeypatch.setattr(greeks_lib, "_compute_opengreeks", _skewed)
        with caplog.at_level(logging.WARNING, logger="greeks_lib"):
            greeks_lib.BS([24000, 24200, 10, 30], volatility=15)
            greeks_lib.BS([24000, 24200, 10, 30], volatility=15)
        divergences = [
            r for r in caplog.records if "callDelta" in r.message
        ]
        assert len(divergences) == 1  # second one rate-limited
        assert greeks_lib._shadow_suppressed.get("callDelta") == 1

    def test_shadow_failure_does_not_break_primary(self, caplog, monkeypatch):
        _use_backend("mibian", shadow=True)

        def _boom(*_args, **_kwargs):
            raise ValueError("synthetic shadow failure")

        monkeypatch.setattr(greeks_lib, "_compute_opengreeks", _boom)
        with caplog.at_level(logging.WARNING, logger="greeks_lib"):
            bs = greeks_lib.BS([24000, 24200, 10, 30], volatility=15)
        assert bs.callDelta is not None
