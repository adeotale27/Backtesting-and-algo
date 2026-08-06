# UI Trading System

A self-hosted algorithmic options-trading dashboard for NIFTY and SENSEX,
built on the Zerodha Kite Connect API.

!!! danger "Read the disclaimer first"
    This software can place **real orders with real money**. It may contain
    bugs and may not behave as intended, and the authors accept **no
    responsibility for any financial loss**. Fresh installs start in
    dry-run mode. Read the **[Disclaimer](disclaimer.md)** in full before
    enabling live trading.

## What you get

- **Live positions** for NIFTY/SENSEX options with delta/theta Greeks and
  margin estimates (embedded Black-Scholes, no external services).
- **Automated strategies**: gap-trading (Wave Extractor), single-leg
  delta-rebalanced Survivor, expiry-day Stoch-RSI trades, and the standalone
  [CAS Expiry](modules/cas-expiry.md) Closing Auction Session algo.
- **Risk tooling**: GTT monitor, duplicate-order detection, position guard,
  early-exit GTT placement.
- **Trade journal** with FIFO pairing, per-algorithm P&L attribution, and
  Zerodha reconciliation.
- **Notifications** via Telegram and browser Web Push.
- A **web dashboard** for all of it — everything runs on your machine.

## Requirements

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | 3.12 recommended (needed for `pandas_ta` / expiry-trade signals) |
| Zerodha account | With funds and F&O activation for live use |
| Kite Connect app | Paid API subscription from [developers.kite.trade](https://developers.kite.trade) — provides the API key/secret |

## Installation

```bash
git clone https://github.com/Raahi-Bhushan/ui-trading-system.git
cd ui-trading-system
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python flask_app.py
```

Open <http://127.0.0.1:5010/>. On first run you are redirected to the
**setup wizard** (`/setup`), which collects:

1. your Kite Connect API key and secret,
2. a dashboard username and password (stored as a hash),
3. whether to enable live trading — **leave this off** until you have
   verified your setup.

It writes `configfile.ini` next to the app. Restart, log in at
`/app_login`, then click **Connect Kite** in the header to complete the
Zerodha OAuth login and start the session.

## First session checklist

1. Header shows 🟡 **DRY-RUN** — good, orders are simulated.
2. Click **Sync Instruments** (⚡ menu) once to populate the instrument
   cache (`instruments.db`, refreshed daily at 9 AM IST afterwards).
3. Open **NIFTY Positions** — your live positions should load with Greeks.
4. Try starting a strategy; watch the logs — every simulated order is
   logged with full parameters.
5. Only when everything behaves: set `[safety] live_trading = true` in
   `configfile.ini`, restart, and the badge turns 🔴 **LIVE**.

## Where to next

- [Configuration reference](configuration.md) — every `configfile.ini` key.
- [Architecture](architecture.md) — how the pieces fit together.
- [Security](security.md) — **read before exposing the app beyond localhost.**
- [FAQ](faq.md)
