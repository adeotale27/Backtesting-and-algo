# UI Trading System — Emergent-hosted adaptation

## Original problem statement
User cloned the `ui-trading-system` repo (Flask + Zerodha Kite Connect algo trading dashboard for NIFTY/SENSEX options). Asked to: (1) explore repo, (2) run it, (3) bypass gatekeeper, (4) build custom credentials input screen, (5) add live↔paper toggle with paper capital allocation, (6) verify modules work with live data.

## Architecture (adapted)
- **Flask app** (Waitress) on 0.0.0.0:3000 (supervisor "frontend" slot) — the real dashboard.
- **FastAPI reverse-proxy** on :8001 (supervisor "backend" slot) — forwards every request to Flask :3000 so all `/api/*` and `/api-*` routes reach the Flask app through Emergent's ingress rule.
- SQLite (`instruments.db`) + APScheduler background jobs.
- Blueprints: notifications, covered_calls, position_guard, setup_wizard.

## Files added / modified
- `/app/flask_app.py` — env-var HOST/PORT, gatekeeper bypass in `enforce_auth`, `/creds` route, `/creds/toggle-mode`, `/creds/paper-capital`, root redirect logic.
- `/app/templates/creds.html` — new custom credentials screen with dark UI, JetBrains Mono headings, live/paper segmented toggle, paper capital input with ₹1L/5L/10L/25L/1Cr quick picks, Kite session status probe.
- `/app/configfile.ini` — real Kite creds, `[safety] live_trading = false`, `[paper_trading] capital = 500000`.
- `/app/frontend/package.json` — shim: `yarn start` launches Flask.
- `/app/backend/server.py` — FastAPI reverse-proxy (via httpx) to Flask.
- `/app/memory/PRD.md`, `/app/memory/test_credentials.md`.

## Features working
- ✅ Dashboard reachable at preview URL, no login (gatekeeper bypassed).
- ✅ `/creds` screen: view masked credentials, update key/secret/request_token/access_token, live Kite `profile()` probe.
- ✅ **Trading mode toggle** — Paper (default) ↔ Live. Confirmation dialog on Live. Hot-reloads `common_lib.live_trading_enabled` without restart.
- ✅ **Paper trading capital** input (shown only in paper mode), quick-pick preset buttons, persists to `[paper_trading] capital` in configfile.ini.
- ✅ Kite session probe: verified as Aniket Damodhar Deotale (HV7316).
- ✅ NIFTY Positions dashboard (shows 0 — user has no current positions).
- ✅ SENSEX Positions dashboard.
- ✅ API Monitor: 3 real calls tracked (instruments, positions, profile) with latency.
- ✅ Trade Journal analytics UI with P&L charts, attribution, category breakdown.
- ✅ WebSocket "Connected" badge showing in header.

## Known small issues
- `-₹NaN` under "Today's Position Taken → Margin Increased/Reduced" on NIFTY Positions when there are no positions (division by zero in template). Cosmetic.
- Access token expires end-of-trading-day; user pastes fresh one at `/creds`.

## Backlog / P0-P2
- P1: One-click Kite OAuth reconnect (opens popup, catches `request_token`, auto-exchanges).
- P1: Wire paper capital into actual margin checks (currently just stored; not read by strategy modules yet).
- P2: Fix `-₹NaN` cosmetic bug on positions dashboard.
- P2: Backtesting engine, ML signal layer, Docker/CI (called out in README).
