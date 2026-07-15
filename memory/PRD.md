# UI Trading System — Emergent-hosted adaptation

## Original problem statement
Zerodha KiteConnect algo trading dashboard (Flask). User asked to: run it, bypass gatekeeper, build custom cred screen, add live/paper toggle + paper capital, verify modules, add a Backtest window under Analysis & Research (date range + strategy + funds → % return), and redesign creds section with tabs + Demo/Live buttons.

## Files added / modified
- `flask_app.py` — gatekeeper bypass, `/creds`, `/creds/toggle-mode`, `/creds/paper-capital`, `/backtest`, `/api/backtest/run` (CSRF-exempt).
- `templates/creds.html` — full custom UI (dark, JetBrains Mono) with: PAPER/LIVE toggle, paper capital ₹1L/5L/10L/25L/1Cr quick picks, Kite session probe, tabbed cred input (`I have access_token` / `Generate from request_token`), Use Demo / Save & Go Live buttons.
- `templates/backtest.html` — new backtest UI: strategy dropdown, index dropdown (NIFTY/BANKNIFTY/SENSEX), date range with quick-pick buttons (Jun 2026 / May 2026 / Q2 / YTD / 1Y / 6M), fund allocation ₹10L/25L/50L/1Cr, KPI cards, Chart.js equity curve, trades table.
- `templates/home.html` — added Backtest card under Analysis & Research.
- `backtest_engine.py` — new module: fetches Kite `historical_data`, 5 strategy proxies (buy&hold, wave, survivor straddle, expiry-RSI, covered calls), returns full BacktestResult (KPIs, equity curve, trades).
- `configfile.ini` — real Kite creds, `[safety] live_trading = false`, `[paper_trading] capital = 500000`.
- `backend/server.py` — FastAPI reverse-proxy to Flask on :3000.
- `frontend/package.json` — shim: yarn start launches Flask on 0.0.0.0:3000.

## Verified working
- ✅ Gatekeeper bypass. Dashboard reachable directly.
- ✅ `/creds` — probe passes (profile: HV7316), toggle, paper capital, tabs, Demo/Live buttons.
- ✅ `/backtest` — full E2E ran Covered Calls on NIFTY / Jun 2026 / ₹50L in 306 ms → +1.44% (₹71,931). All 5 strategies produced results.
- ✅ Modules: NIFTY Positions, SENSEX Positions, Trade Journal, API Monitor (3 real Kite calls tracked).

## June 2026 NIFTY backtest results (₹50L capital, verified)
| Strategy       | Return   | P&L        | Trades | Win rate | Max DD |
|----------------|---------:|-----------:|-------:|---------:|-------:|
| Buy & Hold     |  +1.93%  | ₹96,630   |   1    | 100.0%   | 1.44%  |
| Covered Calls  |  +1.44%  | ₹71,931   |   5    | 100.0%   | 0.68%  |
| Survivor       |  +0.75%  | ₹37,361   |   4    | 100.0%   | 0.00%  |
| Wave Extractor |  +0.13%  | ₹6,417    |   5    |  40.0%   | 0.36%  |
| Expiry (RSI)   |  +0.00%  | ₹0        |   0    |   —      | 0.00%  |

## Caveats
- Backtest engine uses SIMPLIFIED PROXY logic on daily underlying candles — options-chain history isn't available in this Kite subscription tier. Results are directional, not precise.
- Expiry Trade proxy uses daily RSI(14) as a stand-in for the real 3-min Stoch RSI; needs longer windows to trigger signals.
- Access tokens expire end-of-day; paste fresh at /creds.

## Backlog
- P1: Realistic options backtesting (would need historical options-chain data or a paid market-data source).
- P1: Wire paper capital into the strategy modules' pre-trade margin checks.
- P2: One-click Kite OAuth reconnect popup.
- P2: `-₹NaN` cosmetic bug on positions dashboard when zero positions.
