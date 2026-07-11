# Survivor

Dashboard: **`/survivor`**

A single-leg index options strategy with delta-based rebalancing. Each
instance runs as its own process (`place_order_at_nifty.py`,
`place_order_at_sensex.py`, `place_order_at_stock.py`, or their
`_with_buy_auto` variants), selected by index and mode when you start it
from the dashboard.

- **Start**: `POST /api/survivor/start` with index type, mode, expiry,
  strikes, and quantities. The dashboard lists valid expiries per symbol
  (`/api/survivor/symbol_expiries`).
- **Delta rebalancing**: `survivor_delta_rebalance.py` adjusts the
  position as the underlying moves, within the per-symbol bounds from
  `delta_limits.json`.
- **Events**: every decision is recorded to `survivor_events_db.py`
  (SQLite) and streamed to the dashboard (`/api/survivor/events/<pid>`).
- **Logs**: per-instance files under `../survivor_logs/`, viewable from
  the dashboard.
- **Stop / cleanup**: `POST /api/survivor/stop`, and
  `delete_all_stopped` to clear finished instances.

The process table is ground truth: the dashboard PID-verifies instances so
stale status files never show as running.
