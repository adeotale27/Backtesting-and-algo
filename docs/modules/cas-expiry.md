# CAS Expiry Algo

Standalone app: `cas_expiry/` — see [`cas_expiry/README.md`](../../cas_expiry/README.md).

## What it does

On F&O expiry days under SEBI’s Closing Auction Session (CAS, from 3 Aug 2026):

1. Admin **manually activates** the algo.
2. Runner watches Zerodha quotes at ~50ms during 15:28–15:35 IST.
3. When today’s closing price prints (`ohlc.close` updates), it market-sells
   **ATM+1 CE** and **ATM−1 PE** to capture collapsing near-ATM premium.

## Run

```bash
cp cas_expiry/config.ini.example cas_expiry/config.ini
# fill api_key / api_secret / admin password
python -m cas_expiry
```

UI: <http://127.0.0.1:5020/>

## Backtest

Admin UI → Backtest panel, or `cas_expiry.backtest.run_cas_backtest(...)`.

Proxy model only (daily close ≈ CAS price; BS entry premium; intrinsic settlement).
