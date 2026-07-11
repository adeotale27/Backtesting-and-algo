# Early Exit

Dashboards: **`/early-exit`** (NIFTY) and **`/early-exit-sensex`** (SENSEX)

At market open, option prices often have chaotic price discovery (IV
spikes, gap effects). This tool estimates the **fair value** of your open
short option positions and places **GTT buy orders ~10% below** it, so
positions exit at a favourable price if the market cooperates.

## How fair value is computed

- **Pre-open (before 9:15)**: underlying estimated from futures basis +
  spot LTP average; option fair value from Black-Scholes with IV
  back-solved from the previous *trading* day's 3:27 PM minute candle
  (holiday-aware lookback).
- **After open**: 5-candle rolling average of the underlying.

## Flow (preview-first)

1. `GET /early-exit/preview` — computes the table: each qualifying short
   position (current-week expiry, CE/PE only), its fair value, and the
   proposed GTT trigger. **Nothing is placed.**
2. Untick any rows you want to skip.
3. `POST /early-exit/run` — places the GTTs for the confirmed list.
4. `POST /early-exit/run-active` — variant that acts on the active
   selection.

Implementation: `early_exit_lib.py` / `early_exit_sensex_lib.py`.
