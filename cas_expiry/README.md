"""CAS Expiry Algo — SEBI Closing Auction Session premium capture.

Standalone Zerodha algo trading app. On expiry days, after you **manually
activate** it as admin, it watches the CAS close window
(≈15:28–15:35 IST) at millisecond latency and market-sells **ATM+1 Call**
and **ATM−1 Put** the instant the official closing price appears on Kite.

> **This software can trade real money.** Default is dry-run
> (`live_trading = false`). Read the repo root `DISCLAIMER.md` before going live.

## Why this exists

From 3 Aug 2026, SEBI’s Closing Auction Session (CAS) sets the close for
F&O stocks by matching the max-executable-volume equilibrium price in a
short auction after 15:15. Index / option sellers see OTM premiums collapse
within 1–2 minutes of that print. This app’s job is to be armed and faster
than that collapse.

## Quick start

```bash
cd /path/to/ui-trading-system
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install flask waitress   # if not already installed

# Create config from example
cp cas_expiry/config.ini.example cas_expiry/config.ini
# Edit api_key, api_secret, admin password

python -m cas_expiry
```

Open <http://127.0.0.1:5020/> → log in with `[admin]` credentials → paste
today’s Kite `access_token` → **Activate** on expiry day.

## Strategy

| Step | Behaviour |
|------|-----------|
| Arm | Admin clicks **Activate** (nothing auto-arms on boot) |
| Baseline | Before the window, snapshot `ohlc.close` (previous day) |
| Watch | 15:28–15:35 IST, poll quote every `poll_interval_ms` (default 50ms) |
| Detect | When `ohlc.close` flips to today’s CAS close → fire |
| Strikes | `ATM = round(close / gap) * gap`; sell CE at ATM+gap, PE at ATM−gap |
| Orders | Market SELL × `lots` on each leg (`NRML` or `MIS`), tag `CAS` |

Indexes: `NIFTY` (gap 50, NFO), `SENSEX` (gap 100, BFO), or `BOTH`.

## Config (`cas_expiry/config.ini`)

| Section | Keys |
|---------|------|
| `[kite]` | `api_key`, `api_secret`, `access_token` |
| `[admin]` | UI username / password |
| `[strategy]` | `index`, `lots`, `product`, `ce_offset`, `pe_offset`, `require_expiry_today` |
| `[cas_window]` | `watch_start`, `watch_end`, `poll_interval_ms` |
| `[safety]` | `live_trading = false` (default) |
| `[server]` | `host`, `port` (default `127.0.0.1:5020`) |
| `[backtest]` | `default_capital`, `assumed_iv` |

## Admin UI

- Save Kite credentials / exchange request_token
- Activate / Deactivate / Reset day
- Toggle live trading (confirm dialog)
- Manual fire (explicit close or current LTP) for dry-run rehearsal
- Backtest panel (expiry-day CAS proxy)

## Backtest

```bash
python -c "
from datetime import date
from cas_expiry.backtest import run_cas_backtest
r = run_cas_backtest(None, 'NIFTY', date(2026,5,1), date(2026,7,31), 500000)
print(r.total_return_pct, r.num_trades, r.total_pnl)
"
```

Model (directional, not microstructure-accurate):

1. Each weekly expiry day’s close ≈ CAS equilibrium  
2. Entry premium = Black–Scholes with ~5 minutes of time left  
3. Both legs settle to intrinsic at the same close  

Without a Kite session the engine uses a synthetic random-walk path so you
can validate wiring offline.

## Layout

```
cas_expiry/
  app.py              Flask admin UI
  runner.py           Background activation loop
  strategy.py         Detector → strikes → executor
  cas_detector.py     Low-latency close watcher
  strikes.py          ATM±N resolution (reuses instruments.db)
  executor.py         Market SELL (+ dry-run)
  kite_session.py     Thin KiteConnect wrapper (no common_lib import)
  backtest.py         CAS ATM±1 proxy backtest
  config.ini.example  Template credentials / knobs
  templates/          Admin UI
  tests/              Unit tests
```

Reuses from the parent repo when present: vendored `kiteconnect`,
`instrument_cache` (instruments.db + token persistence). Does **not** load
`common_lib` (avoids Flask/dashboard side effects).

## Tests

```bash
pytest cas_expiry/tests/ -q
```

## Safety

1. Fresh installs are dry-run — orders log `[DRY-RUN]` and return `order_id=-1`.  
2. Activation is manual every session intent; fired-today flag prevents double fire.  
3. Bind stays on `127.0.0.1` by default.  
4. Never commit `cas_expiry/config.ini` or `runtime_state.json`.
