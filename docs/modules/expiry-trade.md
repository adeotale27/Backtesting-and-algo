# Expiry Trade

Dashboard: **`/expiry_trade`**

An expiry-day strategy driven by short-term momentum signals:

- Subscribes to real-time ticks (KiteTicker WebSocket) and aggregates them
  into **3-minute candles**.
- Computes **Stochastic RSI** on the candle series (via `pandas_ta`,
  requires Python ≥ 3.12).
- Tracks **support/resistance levels** intraday.
- Entry/exit signals fire on Stoch-RSI crossovers relative to those
  levels; order placement goes through `common_lib` (and therefore honors
  dry-run mode and the GTT/regular order setting).

Endpoints: `POST /api/expiry_trade/start`, plus `/status`, `/candles`,
`/stoch_rsi`, `/support_resistance` for the dashboard's live charts.

Implementation: `expiry_trade_lib.py` (`ExpiryTradeSystem`), instantiated
lazily on first use with the session's Kite client.
