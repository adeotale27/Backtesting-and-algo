# CAS Rule Expiry Automation (solo)

**This branch contains only the CAS Rule algo** — no Survivor, Jodi, covered calls, or other strategies.

WebSocket-first Zerodha automation for SEBI’s **Closing Auction Session (CAS)**:

| Weekday | Index |
|---------|--------|
| Tuesday | NIFTY |
| Thursday | SENSEX |

When the CAS window is **Activated**, KiteTicker streams the index. The moment
`ohlc.close` flips to today’s CAS close, the app **MARKET SELLs** OTM CE + PE
**in parallel** (no limit price).

Default mode is **PAPER** (real Kite data, no orders sent). Read `DISCLAIMER.md`
before switching to live orders.

---

## 1. Setup (Windows / PowerShell)

```powershell
cd C:\Users\USER\OneDrive\Desktop\cas\Backtesting-and-algo

# Use this solo branch
git fetch origin cas-solo-only
git checkout cas-solo-only
git reset --hard origin/cas-solo-only

# Virtualenv (recommended)
python -m venv venv
.\venv\Scripts\Activate.ps1

# Dependencies
pip install -r requirements.txt
# Must include: six, requests, Twisted, autobahn, pyOpenSSL, …
# If you still see "No module named 'six'", your venv is missing deps:
#   pip install six requests python-dateutil pyOpenSSL service-identity "autobahn[twisted]==19.11.2" Twisted
```

## 2. Config

```powershell
copy cas_rule_expiry_automation\config.ini.example cas_rule_expiry_automation\config.ini
notepad cas_rule_expiry_automation\config.ini
```

Set at least:

```ini
[kite]
api_key = YOUR_KITE_API_KEY
api_secret = YOUR_KITE_API_SECRET
access_token =

[admin]
username = admin
password = CHANGE_ME

[strategy]
lots = 1
product = NRML

[safety]
live_trading = false
paper_any_day = true
```

`config.ini` is gitignored — never commit it.

## 3. Run

```powershell
python -m cas_rule_expiry_automation
```

Open: **http://127.0.0.1:5030**

Login with the admin username/password from `config.ini`.

## 4. Daily use (Live page)

1. Click **Kite API** → paste today’s `access_token` → save  
2. Confirm **PAPER** (or switch to LIVE only when you intend real orders)  
3. Set **Lots per leg** (e.g. 10 → sells 10 CE + 10 PE)  
4. Click **Activate CAS window**  
   - **Last close** was pulled once at app start  
   - **LTP** starts streaming only after Activate  
5. On close flip (~15:28–15:30 IST) → parallel MARKET CE+PE  
6. **Deactivate** when done (stops WebSocket / clears LTP)

Paper works on non-expiry days too (`paper_any_day=true`).  
**LIVE** money still waits for Tue NIFTY / Thu SENSEX when `expiry_only=true`.

## 5. Backtest page

Top nav → **Backtest** → pick dates (defaults to today) → Run.  
Same strike + MARKET path as live; no orders are sent.

## 6. Tests

```powershell
$env:PYTHONPATH="."
python -m pytest cas_rule_expiry_automation/tests/ -q
```

## 7. What this repo contains

```
cas_rule_expiry_automation/   # the only strategy
vendor/pykiteconnect/         # Zerodha Kite Connect SDK (vendored)
README.md                     # this file
DISCLAIMER.md
LICENSE
requirements.txt
```

Everything else (Survivor, place_order_at_*, flask_app, covered calls, etc.)
was removed from **cas-solo-only**.

## Strike rule (reminder)

| Spot vs ATM | CE sold | PE sold |
|-------------|---------|---------|
| Spot **below** ATM | **ATM CE** | ATM − N |
| Spot **above** ATM | ATM + N | **ATM PE** |
| Spot **exact** ATM | ATM + N | ATM − N |

## Safety

- `live_trading = false` by default (PAPER)  
- MARKET orders use `market_protection=-1` (AUTO); no `price` / `trigger_price`  
- Prefer a Mumbai VPS (`ap-south-1`) for lowest practical latency to Zerodha  
