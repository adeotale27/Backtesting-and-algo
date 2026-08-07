"""Runtime dependency checks for vendored kiteconnect."""

from __future__ import annotations

from typing import List, Tuple

# Modules required to import vendor/pykiteconnect (KiteConnect + KiteTicker).
_REQUIRED: Tuple[Tuple[str, str], ...] = (
    ("six", "six"),
    ("requests", "requests"),
    ("dateutil", "python-dateutil"),
    ("OpenSSL", "pyOpenSSL"),
    ("service_identity", "service-identity"),
    ("twisted", "Twisted"),
    ("autobahn", "autobahn[twisted]==19.11.2"),
)


def missing_kite_packages() -> List[str]:
    missing: List[str] = []
    for mod, pip_name in _REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    return missing


def ensure_kite_deps() -> None:
    """Raise a clear error if kiteconnect runtime deps are missing."""
    missing = missing_kite_packages()
    if not missing:
        # Also prove vendor kiteconnect imports
        try:
            from kiteconnect import KiteConnect  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"kiteconnect import failed ({exc}). "
                "Ensure ./vendor/pykiteconnect is on PYTHONPATH "
                "(running `python -m cas_rule_expiry_automation` from repo root does this)."
            ) from exc
        return

    pkgs = " ".join(missing)
    raise RuntimeError(
        "Missing Python packages required by Zerodha kiteconnect: "
        f"{', '.join(missing)}.\n\n"
        "Fix (from the repo root, with your venv active):\n"
        "  pip install -r requirements.txt\n"
        f"  # or: pip install {pkgs}\n"
        "Then restart: python -m cas_rule_expiry_automation"
    )
