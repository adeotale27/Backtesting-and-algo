"""Configuration loader for the CAS Expiry algo."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from datetime import time
from typing import List, Optional


_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(_PKG_DIR, "config.ini")
EXAMPLE_CONFIG_PATH = os.path.join(_PKG_DIR, "config.ini.example")
STATE_PATH = os.path.join(_PKG_DIR, "runtime_state.json")


def _parse_hhmmss(value: str) -> time:
    parts = [int(p) for p in value.strip().split(":")]
    if len(parts) == 2:
        return time(parts[0], parts[1])
    if len(parts) == 3:
        return time(parts[0], parts[1], parts[2])
    raise ValueError(f"Invalid time value: {value!r} (expected HH:MM or HH:MM:SS)")


@dataclass
class CasConfig:
    """Typed view over cas_expiry/config.ini."""

    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""

    admin_username: str = "admin"
    admin_password: str = "CHANGE_ME"

    index: str = "NIFTY"
    lots: int = 1
    product: str = "NRML"
    ce_offset: int = 1
    pe_offset: int = 1
    require_expiry_today: bool = True

    watch_start: time = field(default_factory=lambda: time(15, 28, 0))
    watch_end: time = field(default_factory=lambda: time(15, 35, 0))
    poll_interval_ms: int = 50
    idle_poll_seconds: float = 5.0

    live_trading: bool = False

    host: str = "127.0.0.1"
    port: int = 5020

    default_capital: float = 500_000.0
    assumed_iv: float = 18.0

    config_path: str = DEFAULT_CONFIG_PATH

    def indexes(self) -> List[str]:
        """Return the list of underlyings this run targets."""
        raw = (self.index or "NIFTY").strip().upper()
        if raw == "BOTH":
            return ["NIFTY", "SENSEX"]
        if raw in ("NIFTY", "SENSEX"):
            return [raw]
        raise ValueError(f"Unsupported index={raw!r}; use NIFTY, SENSEX, or BOTH")

    @property
    def poll_interval_seconds(self) -> float:
        return max(self.poll_interval_ms, 1) / 1000.0


def ensure_config(path: Optional[str] = None) -> str:
    """Create config.ini from the example if missing. Returns the path used."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(cfg_path):
        if not os.path.exists(EXAMPLE_CONFIG_PATH):
            raise FileNotFoundError(
                f"Missing both {cfg_path} and {EXAMPLE_CONFIG_PATH}"
            )
        with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as src:
            contents = src.read()
        with open(cfg_path, "w", encoding="utf-8") as dst:
            dst.write(contents)
    return cfg_path


def load_config(path: Optional[str] = None) -> CasConfig:
    """Load and parse cas_expiry config.ini (creates from example if needed)."""
    cfg_path = ensure_config(path)
    parser = configparser.ConfigParser()
    parser.read(cfg_path)

    return CasConfig(
        api_key=parser.get("kite", "api_key", fallback="").strip(),
        api_secret=parser.get("kite", "api_secret", fallback="").strip(),
        access_token=parser.get("kite", "access_token", fallback="").strip(),
        admin_username=parser.get("admin", "username", fallback="admin").strip(),
        admin_password=parser.get("admin", "password", fallback="CHANGE_ME"),
        index=parser.get("strategy", "index", fallback="NIFTY").strip().upper(),
        lots=parser.getint("strategy", "lots", fallback=1),
        product=parser.get("strategy", "product", fallback="NRML").strip().upper(),
        ce_offset=parser.getint("strategy", "ce_offset", fallback=1),
        pe_offset=parser.getint("strategy", "pe_offset", fallback=1),
        require_expiry_today=parser.getboolean(
            "strategy", "require_expiry_today", fallback=True
        ),
        watch_start=_parse_hhmmss(
            parser.get("cas_window", "watch_start", fallback="15:28:00")
        ),
        watch_end=_parse_hhmmss(
            parser.get("cas_window", "watch_end", fallback="15:35:00")
        ),
        poll_interval_ms=parser.getint("cas_window", "poll_interval_ms", fallback=50),
        idle_poll_seconds=parser.getfloat(
            "cas_window", "idle_poll_seconds", fallback=5.0
        ),
        live_trading=parser.getboolean("safety", "live_trading", fallback=False),
        host=parser.get("server", "host", fallback="127.0.0.1").strip(),
        port=parser.getint("server", "port", fallback=5020),
        default_capital=parser.getfloat(
            "backtest", "default_capital", fallback=500_000.0
        ),
        assumed_iv=parser.getfloat("backtest", "assumed_iv", fallback=18.0),
        config_path=cfg_path,
    )


def save_kite_credentials(
    api_key: str,
    api_secret: str,
    access_token: str = "",
    path: Optional[str] = None,
) -> None:
    """Persist Kite credentials into config.ini (creates file if needed)."""
    cfg_path = ensure_config(path)
    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    if not parser.has_section("kite"):
        parser.add_section("kite")
    parser.set("kite", "api_key", api_key.strip())
    parser.set("kite", "api_secret", api_secret.strip())
    if access_token is not None:
        parser.set("kite", "access_token", access_token.strip())
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        parser.write(fh)
    os.replace(tmp, cfg_path)


def set_live_trading(enabled: bool, path: Optional[str] = None) -> None:
    """Toggle [safety] live_trading in config.ini."""
    cfg_path = ensure_config(path)
    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    if not parser.has_section("safety"):
        parser.add_section("safety")
    parser.set("safety", "live_trading", "true" if enabled else "false")
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        parser.write(fh)
    os.replace(tmp, cfg_path)
