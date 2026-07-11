# Wave Extractor

Dashboard: **`/wave-extractor`**

A continuous options gap-trading strategy. One **scraper process**
(`ticker_single_scraper_new.py`) runs per trading symbol:

1. At startup it places a pair of limit orders — BUY at
   `price − buy_gap`, SELL at `price + sell_gap`.
2. When one leg fills, the sibling is cancelled and a fresh pair is placed.
3. The cycle repeats all session; each fill "extracts" one wave of the
   price oscillation.
4. Gaps are scaled by a **multiplier stack** (step → delta → velocity)
   that widens or narrows the bracket based on position risk and momentum.

## Lifecycle

```
POST /api/start  {symbol, buy_gap, sell_gap, buy_quantity, sell_quantity}
  → spawns ticker_single_scraper_new.py (one PID per symbol)
  → logs to ../logs/<SYMBOL>.log
  → status to status/status_<SYMBOL>.json (drives the dashboard)
```

- A **duplicate-instance guard** rejects a second start for the same
  symbol (409) unless `force` is passed — two scrapers on one symbol would
  double the legs.
- Every 180 s the scraper polls order state (`check_orders()`) — the
  WebSocket `on_order_update` callback is fast-path only; polling is the
  authoritative fallback (COMPLETE can arrive before OPEN, especially for
  GTT-fired orders).
- `/api/stop`, `/api/stop_group`, `/api/stop_all` stop instances by PID,
  optionally cancelling their open orders.

## GTT fallback

If a duo order fails (circuit limit / margin), a GTT is placed as
fallback. When it fires, the new order id is linked back to the strategy's
order state via tag echo.
