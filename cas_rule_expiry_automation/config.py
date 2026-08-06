"""Config loader for CAS Rule Expiry Automation."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from datetime import time
from typing import Optional

_PKG = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(_PKG, "config.ini")
EXAMPLE_CONFIG_PATH = os.path.join(_PKG, "config.ini.example")
STATE_PATH = os.path.join(_PKG, "runtime_state.json")


def _parse_time(value: str) -> time:
    parts = [int(p) for p in value.strip().split(":")]
    if len(parts) == 2:
        return time(parts[0], parts[1])
    if len(parts) != 3:
        raise ValueError(f"Bad time: {value!r}")
    return time(parts[0], parts[1], parts[2])


@dataclass
class AppConfig:
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""

    admin_username: str = "admin"
    admin_password: str = "CHANGE_ME"

    lots: int = 1
    ce_otm_steps: int = 1
    pe_otm_steps: int = 1
    product: str = "NRML"
    expiry_only: bool = True
    nifty_expiry_weekday: int = 1  # Tuesday
    sensex_expiry_weekday: int = 3  # Thursday

    ws_mode: str = "full"
    fire_on_close_update: bool = True
    fire_on_ltp_in_window: bool = False
    prewarm_minutes: int = 12
    strike_resolve_budget_ms: int = 5

    watch_start: time = field(default_factory=lambda: time(15, 27))
    watch_end: time = field(default_factory=lambda: time(15, 35))

    live_trading: bool = False
    host: str = "127.0.0.1"
    port: int = 5030

    default_capital: float = 500_000.0
    assumed_iv: float = 35.0
    # Optional synthetic floor (₹). Default 0 — prefer real Kite option LTPs.
    assumed_cas_otm_premium: float = 0.0
    # Minutes of time-value assumed remaining when CAS close prints (BS fallback only)
    entry_time_minutes: float = 15.0
    tick_interval_ms: int = 100

    config_path: str = DEFAULT_CONFIG_PATH


def ensure_config(path: Optional[str] = None) -> str:
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(cfg_path):
        with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as src:
            data = src.read()
        with open(cfg_path, "w", encoding="utf-8") as dst:
            dst.write(data)
    return cfg_path


def load_config(path: Optional[str] = None) -> AppConfig:
    cfg_path = ensure_config(path)
    p = configparser.ConfigParser()
    p.read(cfg_path)

    # Migrate older installs that still watch from 15:28 — must start at 15:27
    # so a 15:28:00 CAS print is never missed.
    raw_start = p.get("cas_window", "watch_start", fallback="15:27:00").strip()
    if raw_start in ("15:28:00", "15:28", "15:28:0"):
        if not p.has_section("cas_window"):
            p.add_section("cas_window")
        p.set("cas_window", "watch_start", "15:27:00")
        tmp = cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            p.write(fh)
        os.replace(tmp, cfg_path)
        raw_start = "15:27:00"

    return AppConfig(
        api_key=p.get("kite", "api_key", fallback="").strip(),
        api_secret=p.get("kite", "api_secret", fallback="").strip(),
        access_token=p.get("kite", "access_token", fallback="").strip(),
        admin_username=p.get("admin", "username", fallback="admin").strip(),
        admin_password=p.get("admin", "password", fallback="CHANGE_ME"),
        lots=p.getint("strategy", "lots", fallback=1),
        ce_otm_steps=p.getint("strategy", "ce_otm_steps", fallback=1),
        pe_otm_steps=p.getint("strategy", "pe_otm_steps", fallback=1),
        product=p.get("strategy", "product", fallback="NRML").strip().upper(),
        expiry_only=p.getboolean("strategy", "expiry_only", fallback=True),
        nifty_expiry_weekday=p.getint("strategy", "nifty_expiry_weekday", fallback=1),
        sensex_expiry_weekday=p.getint(
            "strategy", "sensex_expiry_weekday", fallback=3
        ),
        ws_mode=p.get("latency", "ws_mode", fallback="full").strip().lower(),
        fire_on_close_update=p.getboolean(
            "latency", "fire_on_close_update", fallback=True
        ),
        fire_on_ltp_in_window=p.getboolean(
            "latency", "fire_on_ltp_in_window", fallback=False
        ),
        prewarm_minutes=p.getint("latency", "prewarm_minutes", fallback=12),
        strike_resolve_budget_ms=p.getint(
            "latency", "strike_resolve_budget_ms", fallback=5
        ),
        watch_start=_parse_time(raw_start or "15:27:00"),
        watch_end=_parse_time(p.get("cas_window", "watch_end", fallback="15:35:00")),
        live_trading=p.getboolean("safety", "live_trading", fallback=False),
        host=p.get("server", "host", fallback="127.0.0.1").strip(),
        port=p.getint("server", "port", fallback=5030),
        default_capital=p.getfloat("backtest", "default_capital", fallback=500_000),
        assumed_iv=p.getfloat("backtest", "assumed_iv", fallback=35.0),
        assumed_cas_otm_premium=p.getfloat(
            "backtest", "assumed_cas_otm_premium", fallback=0.0
        ),
        entry_time_minutes=p.getfloat(
            "backtest", "entry_time_minutes", fallback=15.0
        ),
        tick_interval_ms=p.getint("backtest", "tick_interval_ms", fallback=100),
        config_path=cfg_path,
    )


def save_section_values(section: str, values: dict, path: Optional[str] = None) -> None:
    cfg_path = ensure_config(path)
    p = configparser.ConfigParser()
    p.read(cfg_path)
    if not p.has_section(section):
        p.add_section(section)
    for k, v in values.items():
        p.set(section, k, str(v))
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        p.write(fh)
    os.replace(tmp, cfg_path)


def save_kite_credentials(
    api_key: str, api_secret: str, access_token: str = "", path: Optional[str] = None
) -> None:
    save_section_values(
        "kite",
        {
            "api_key": api_key.strip(),
            "api_secret": api_secret.strip(),
            "access_token": (access_token or "").strip(),
        },
        path,
    )


def save_strategy_settings(
    lots: int,
    ce_otm_steps: int,
    pe_otm_steps: int,
    product: str = "NRML",
    path: Optional[str] = None,
) -> None:
    save_section_values(
        "strategy",
        {
            "lots": int(lots),
            "ce_otm_steps": int(ce_otm_steps),
            "pe_otm_steps": int(pe_otm_steps),
            "product": product.strip().upper(),
        },
        path,
    )


def set_live_trading(enabled: bool, path: Optional[str] = None) -> None:
    save_section_values("safety", {"live_trading": "true" if enabled else "false"}, path)
