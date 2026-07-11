# Trade Journal

Dashboard: **`/trade-journal`**

A daily trading journal built automatically from your order flow:

- **FIFO pairing** of buys and sells per symbol into round-trip trades.
- **Algo attribution** — each trade is classified by source (Wave
  Extractor, Survivor, manual, stop-loss) from order tags.
- **P&L per day / per algo**, with settlement handling for positions
  carried to expiry (underlying close prices fetched via Kite only).
- **Zerodha reconciliation** (`POST /api/trade_journal/reconcile`) — pulls
  the broker's orderbook and adds anything missing (e.g. manual trades
  placed from the Kite app), de-duplicated by order id and fuzzy matching.
- **Notes and market conditions** per day
  (`/api/trade_journal/notes`, `/api/trade_journal/market_conditions`).
- **Position validation** — cross-checks journal-derived open positions
  against the broker's actual positions.

Backed by a SQLite cache (`trade_journal.db`) so historical days load
instantly; `POST /api/trade_journal/rebuild` regenerates from the raw
order files.

## Tradebook Analysis

Dashboard: **`/tradebook-analysis`**

Upload a Zerodha tradebook CSV export and get per-expiry aggregation
(weekly/monthly), margin estimates, and P&L summaries — useful for
analyzing history that predates your journal.
