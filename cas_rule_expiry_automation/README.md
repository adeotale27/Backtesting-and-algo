# CAS Rule Expiry Automation

WebSocket-first Zerodha algo for SEBI’s **Closing Auction Session** (CAS).

**Expiry only**

| Weekday | Index |
|---------|--------|
| Tuesday | NIFTY |
| Thursday | SENSEX |

When armed, KiteTicker (`MODE_FULL`) streams the index. The instant
`ohlc.close` flips to today’s CAS print (or optional LTP-in-window mode),
the app market-sells configurable **OTM** Call + Put (default ATM±1).

> True zero latency over the public internet is impossible. Lowest practical
> latency = official Kite WebSocket + VPS in **Mumbai (ap-south-1)** +
> pre-warmed strikes + no extra quote round-trip on fire.

Default is **dry-run**. Read the repo `DISCLAIMER.md` before live trading.

## Quick start

```bash
cd /path/to/repo
pip install -r requirements.txt
pip install -r cas_rule_expiry_automation/requirements.txt

cp cas_rule_expiry_automation/config.ini.example cas_rule_expiry_automation/config.ini
# set api_key, api_secret, admin password, lots

python -m cas_rule_expiry_automation
```

Open **http://127.0.0.1:5030**

1. Save Kite credentials / today’s `access_token`
2. Set **lots** and OTM steps
3. On Tuesday or Thursday → **Activate**
4. Engine pre-warms strikes ~12 min before 15:28, opens WebSocket, fires on close

## Configurable knobs (`config.ini`)

```ini
[strategy]
lots = 1                 # lots per leg (CE and PE)
ce_otm_steps = 1         # ATM + N strike steps (Call)
pe_otm_steps = 1         # ATM - N strike steps (Put)
product = NRML
expiry_only = true
nifty_expiry_weekday = 1   # Tuesday
sensex_expiry_weekday = 3  # Thursday

[latency]
ws_mode = full
fire_on_close_update = true
fire_on_ltp_in_window = false
prewarm_minutes = 12
```

All of `lots` / OTM steps / product are also editable in the UI.

## Timing board (live + backtest)

Every fire persists and shows:

| Field | Meaning |
|-------|---------|
| `cas_detected_at` | Exact IST timestamp when CAS close appeared (~15:28–15:30) |
| `ce_sold_at` | When ATM+N Call market sell was submitted |
| `pe_sold_at` | When ATM−N Put market sell was submitted |
| `detect_to_ce_ms` / `detect_to_pe_ms` | Milliseconds from detect → each leg |
| `detect_to_done_ms` | Total detect → both legs done |

UI panel: **CAS → Sell latency timeline**. Backtest table includes the same columns.

## Latency design


1. **KiteTicker WebSocket** (not REST polling) for index updates  
2. **MODE_FULL** so `ohlc.close` arrives on the wire  
3. **Pre-warm** nearby CE/PE contracts before the window  
4. **Fire path** resolves from cache + `place_order` MARKET SELL — no quote call  
5. Deploy the process on a **Mumbai** cloud VM for minimum RTT to Zerodha  

## WebSocket backtest

The backtest expands minute (or synthetic) candles into a tick stream and
replays them through the **same** `TickBus` → `on_ticks` contract as live:

```bash
python -c "
from datetime import date
from cas_rule_expiry_automation.backtest_ws import run_ws_backtest
from cas_rule_expiry_automation.config import load_config
r = run_ws_backtest(None, load_config(), date(2026,5,1), date(2026,7,31), 500000)
print(r.num_trades, r.ws_ticks_total, r.total_pnl, r.total_return_pct)
"
```

Or use the **WebSocket backtest** panel in the UI.

## Layout

```
cas_rule_expiry_automation/
  app.py                 Light-theme admin UI (:5030)
  engine.py              Activate → WS → strategy
  ws_stream.py           KiteTicker + tick replay bus
  strategy_engine.py     Close/LTP fire logic
  strike_resolver.py     Pre-warm ATM±N cache
  order_engine.py        Market sell (dry-run aware)
  backtest_ws.py         WS-path backtest
  expiry_calendar.py     Tue NIFTY / Thu SENSEX
  config.ini.example
  static/style.css       Light theme
  templates/
```

## Tests

```bash
PYTHONPATH=. python3 -m pytest cas_rule_expiry_automation/tests/ -q
```

## Safety

- `[safety] live_trading = false` by default  
- Manual **Activate** required — nothing arms on boot alone  
- Per-index fired flag prevents double sells  
- Binds to `127.0.0.1` by default  
