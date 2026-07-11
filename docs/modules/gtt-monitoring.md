# GTT & Order Monitoring

## GTT Monitor

Endpoints: `/gtt_monitor/status`, `/gtt_monitor/debug`,
`/gtt_monitor/check` — also runs every 10 minutes via the notifications
scheduler.

Tracks all GTTs on the account, watches for triggers, and suppresses stale
GTT alerts when the market is closed (holiday- and hours-aware). Fired
GTTs generate notifications so a triggered exit never goes unnoticed.

## Duplicate Order Monitor

Dashboard: **`/duplicate-orders`** — also checked every 5 minutes by the
scheduler.

Detects multiple open orders on the same symbol within ~2% price of each
other (the classic double-legged-strategy accident) and lets you cancel
the duplicate with one click (`POST /api/cancel-duplicate-order`).

## Position Guard

Dashboard: **`/position-guard`**

A review queue of symbols with **unreviewed long exposure** across
positions, open orders, and GTTs combined. Every new exposure appears here
until explicitly reviewed, so nothing accumulates silently. Backed by its
own SQLite store; checked every 5 minutes by the scheduler.

## API Monitor

Dashboard: **`/api-monitor`**

Every Kite API call in the app goes through `MonitoredKite`, a proxy that
records method, calling code path, latency, and errors to
`api_monitor.db`. Use it to spot rate-limit pressure and failing calls
per day.
