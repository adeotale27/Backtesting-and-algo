# UI Trading System

A self-hosted algorithmic options-trading dashboard for NIFTY and SENSEX,
built on the [Zerodha Kite Connect API](https://kite.trade). Flask web UI,
real-time positions with Greeks, GTT monitoring, and several automated
strategies (gap trading, survivor, expiry trades) — everything runs on your
own machine against your own Zerodha account.

> ## ⚠️ Read First
>
> **This software can trade real money.** It is experimental, may contain
> bugs, and may not behave as intended. The authors accept **no
> responsibility for any financial loss** caused by using it. Fresh installs
> start in **dry-run mode** (orders are simulated, nothing reaches the
> broker) — read **[DISCLAIMER.md](DISCLAIMER.md)** in full before enabling
> live trading.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Features

| Module | What it does |
|--------|--------------|
| **Positions** | Live NIFTY/SENSEX option positions with delta/theta Greeks and margin (embedded Black-Scholes) |
| **Wave Extractor** | Gap-trading automation: linked BUY+SELL order pairs re-placed as price waves move |
| **Survivor** | Single-leg index strategy with delta-based rebalancing |
| **Expiry Trade** | Expiry-day strategy on 3-minute candles with Stochastic RSI signals |
| **Early Exit** | Pre-market fair-value GTT exit orders for NIFTY/SENSEX options |
| **GTT Monitor** | Watches GTT triggers, detects duplicates, suppresses stale orders when market is closed |
| **Position Guard** | Flags symbols with unreviewed long exposure across positions, orders, and GTTs |
| **Trade Journal** | FIFO buy/sell pairing, per-algo P&L attribution, Zerodha reconciliation |
| **Covered Calls** | Sell OTM calls against held equity to earn premium |
| **Notifications** | Telegram bot + browser Web Push for fills, margin alerts, and system events |
| **API Monitor** | Every Kite API call logged with caller, latency, and errors |

## Requirements

- Python 3.11+
- A Zerodha account with a [Kite Connect](https://developers.kite.trade/)
  app subscription (paid; needed for the API key/secret)

## Quickstart

```bash
git clone https://github.com/Raahi-Bhushan/ui-trading-system.git
cd ui-trading-system
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python flask_app.py
```

Open <http://127.0.0.1:5010/> — a **setup wizard** walks you through the
Kite API credentials and dashboard login, then writes `configfile.ini` for
you. Restart the app and log in.

The app binds to `127.0.0.1` only. **Never expose it to the internet
without a TLS-terminating reverse proxy in front** — see the
[security notes](docs/security.md).

## Dry-run vs live trading

New installs run with `[safety] live_trading = false`: every order-placement
call is logged with full details but **nothing is sent to Zerodha**. The
header shows a 🟡 DRY-RUN badge. When you are ready, set
`live_trading = true` in `configfile.ini` and restart — the badge turns
🔴 LIVE.

## Documentation

Full docs (configuration reference, architecture, per-module guides, FAQ)
live in [`docs/`](docs/) and are published as a website — see the repository
description for the hosted URL.

## Using the core library without the dashboard

The trading primitives (order placement with retries, instrument cache,
Greeks, GTT monitoring) are pip-installable:

```bash
pip install .
python -c "import instrument_cache, positions_lib, greeks_lib"
```

## Tests

```bash
pytest tests/
```

Tests use real SQLite (no DB mocking) — see
[CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

## Contributing & Security

- Contributions welcome — read [CONTRIBUTING.md](CONTRIBUTING.md) first.
- Found a vulnerability? Please follow [SECURITY.md](SECURITY.md) instead of
  opening a public issue.

## License

[MIT](LICENSE) — with the additional trading-risk terms in
[DISCLAIMER.md](DISCLAIMER.md). Not affiliated with Zerodha.
