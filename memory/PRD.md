# UI Trading System — Emergent-hosted adaptation

## Original problem statement
User cloned the `ui-trading-system` repo (Flask + Zerodha Kite Connect algo trading dashboard, NIFTY/SENSEX options). Asked to: (1) explore repo, (2) tell what could be improved, (3) run it, (4) bypass gatekeeper login, (5) build a custom credentials input screen. Provided real Kite API key, secret, request_token, and access_token.

## Architecture
- **Flask app** (Waitress) — the entire dashboard, runs on 0.0.0.0:3000 (supervisor "frontend" slot).
- **FastAPI stub** on :8001 (supervisor "backend" slot) — only serves /api/health for platform smoke checks.
- **SQLite** (instruments.db) for instrument cache + persisted Kite session token.
- **APScheduler** background jobs (P&L summary, delta checks, GTT monitor, etc.).
- **Blueprints**: notifications, covered_calls, position_guard, setup_wizard.

## Adaptations made for Emergent hosting
1. `pip install -r requirements.txt` + fastapi/uvicorn.
2. `/app/flask_app.py`: `HOST`/`PORT` now env-var overridable.
3. `/app/frontend/package.json` (new): `yarn start` launches Flask on 0.0.0.0:3000.
4. `/app/backend/server.py` (new): minimal FastAPI stub on :8001 with `/api/health`.
5. `/app/configfile.ini` (new): real Kite creds, dry-run mode ON.
6. **Gatekeeper bypass** — `enforce_auth()` now auto-sets `session['app_authenticated']=True` for every request and auto-restores `session['access_token']` from the persisted DB token.
7. **New `/creds` route + template** — custom screen to view status and update api_key/secret/request_token/access_token. Kite session probe (calls `kite.profile()`) confirms creds work.
8. `/` now redirects → `/home` if authed, else → `/creds`.
9. Pre-seeded access token into `kite_session_tokens` table.

## What's working
- Dashboard loads at preview URL, no login required.
- All modules render (Positions, Wave Extractor, Survivor, Expiry Trade, Covered Calls, Early Exit, Trade Journal, API Monitor, Position Guard, Duplicate Orders).
- Kite REST session verified — profile fetched successfully (Aniket Damodhar Deotale / HV7316).
- Dry-run mode active — no real orders will be placed.

## Notes / caveats
- Header shows "Disconnected" — this is the WebSocket ticker (separate stream), not the REST session. It will connect when a module needs live ticks.
- Access token expires end of trading day; user re-pastes new one at `/creds`.
- `webhook-crond` supervisor program is FATAL but unrelated (platform artifact).

## Backlog / P0-P2
- P1: Kick off the KWS ticker on startup so header shows "Connected".
- P1: Auto-refresh access token via `request_token` when the current one 401s.
- P2: Optional — add a "Live trading" toggle to `/creds` (currently requires INI edit).
- P2: Backtesting engine, ML signal layer, paper-trading (called out in original discussion).
- P2: Docker / CI workflow (README hints at these but not committed).
