# Architecture

## Overview

Everything is a single Flask process (served by `waitress`, bound to
`127.0.0.1:5010`) plus optional strategy subprocesses it spawns.

```
Authentication:  Browser → Flask (gatekeeper login) → Kite Connect OAuth
                 → access_token → Flask session + instruments.db (restart recovery)

Order execution: strategy scripts → common_lib.place_order()
                 → [safety] live_trading gate → KiteConnect API
                 → on_order_update() WebSocket callback → JSON state file

Position data:   KiteConnect.positions() → positions_lib → Greeks (mibian/opengreeks)
                 → dashboard

Instruments:     daily 9 AM IST → instrument_cache.sync_instruments() → instruments.db
                 (atomic swap — never open instruments.db directly during sync)
```

## Core modules

| Module | Role |
|--------|------|
| `common_lib.py` | The central engine: order placement/cancellation with retry (`retry_with_backoff()`), gap management, delta calculations, order state persistence, config loading, IST time (`get_ist_now()`) |
| `flask_app.py` | Web server: dashboard routes, REST endpoints, auth, CSRF/rate-limit protection, blueprint registration |
| `instrument_cache.py` | SQLite instrument DB; maps symbols → Kite tokens; daily atomic-swap sync |
| `positions_lib.py` / `sensex_positions_lib.py` | Position summaries with Greeks and margin |
| `greeks_lib.py` | Facade over the embedded `mibian/` Black-Scholes library or the faster `opengreeks` backend (config-selected). Never import `mibian` directly |
| `kite_api_monitor.py` | `MonitoredKite` wraps every KiteConnect call — logs method, caller, latency, errors; dashboard at `/api-monitor` |
| `gtt_monitor.py` / `duplicate_order_monitor.py` | GTT trigger tracking and duplicate-order detection |
| `setup_wizard.py` | First-run web form that writes `configfile.ini` |

## Strategy processes

Strategies that trade continuously run as **separate processes** spawned by
the dashboard, each writing its own log under `../logs/`:

- **Wave Extractor** — `ticker_single_scraper_new.py`, one process per
  symbol; places linked BUY+SELL pairs and re-places them as price moves.
- **Survivor** — `place_order_at_*.py` variants per index/mode; single-leg
  strategy with delta-based rebalancing (`survivor_delta_rebalance.py`).

The dashboard tracks them by PID (the process table is ground truth) and
guards against duplicate instances per symbol.

## Blueprints

| Blueprint | Mounted at | Purpose |
|-----------|-----------|---------|
| `notifications` | `/notifications` | Telegram + Web Push delivery, APScheduler jobs (P&L summary, margin checks, GTT monitor ticks) |
| `covered_calls` | `/covered_calls` | Covered-call strategy dashboard |
| `position_guard` | `/position-guard` | Unreviewed-exposure review queue |
| `setup_wizard` | `/setup` | First-run configuration |

## Key conventions

- **IST everywhere** — `get_ist_now()`, never `datetime.now()`.
- **Retry** all Kite calls that can rate-limit: `retry_with_backoff()`.
- **Instrument tokens change** after corporate actions — always resolve via
  `instrument_cache.get_instrument_token()`; lot sizes are dynamic too.
- **Order state** is persisted as JSON (`executed_orders_<date>.json`) and
  reloaded with `load_todays_orders()`.
- **WebSocket race**: a `COMPLETE` order update can arrive before `OPEN`
  (especially GTT-fired). The 180-second polling loop is the authoritative
  fallback, not just a safety net.
- **GTT fallback recovery**: when a duo order fails, a GTT is placed as
  fallback; GTT-fired orders arrive with a *new* order id which is linked
  back via tag echo.

## About this repository

This public repository is exported from a larger private codebase. Some
experimental modules referenced in older discussions (ML predictors, copy
trading, backtesting) are intentionally not part of the export.
