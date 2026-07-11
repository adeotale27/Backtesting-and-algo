# Positions & Greeks

Dashboards: **`/positions`** (NIFTY) and **`/sensex_positions`** (SENSEX).

Live option positions from `kite.positions()` enriched with:

- **Delta and theta** per leg and aggregated per expiry, computed with
  Black-Scholes (`greeks_lib` — embedded `mibian` by default, or the
  Rust-backed `opengreeks` package via `[greeks] library`).
- **IV** back-solved per option from its last price where possible,
  falling back to `[option_details] current_volatility`.
- **Margin estimates** per position.

Views support current expiry, next expiry, and custom days-to-expiry
what-if calculations (`/api/nifty_positions/custom_days`).

Delta bounds from `delta_limits.json` (editable via `/api/delta_config`)
are enforced by the strategies before hedging orders are placed.
