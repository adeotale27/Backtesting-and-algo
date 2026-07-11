# Notifications

Blueprint: **`/notifications`** — bell icon in the global header on every
page.

Delivery channels (all optional, configured in `[notifications]`):

- **Telegram** — create a bot with BotFather, set `telegram_bot_token`
  and `telegram_chat_id`.
- **Browser Web Push** — VAPID keys; subscribe from the notifications
  page.
- **In-app** — the header bell with complete/ignore actions.

## Scheduled jobs

A background APScheduler (IST timezone) drives recurring checks,
including:

| Job | Schedule |
|-----|----------|
| Kite login reminder | 9:20 AM |
| Early-exit reminder | 9:08 AM |
| Daily P&L summary | 3:35 PM |
| Journal reconciliation | 3:40 PM |
| Delta bounds check | every 15 min |
| GTT monitor | every 10 min |
| Duplicate orders | every 5 min |
| Position guard | every 5 min |
| Notification purge | 4:00 AM |

Jobs never raise — failures are logged and retried on the next tick.
Links inside notifications use `[notifications] server_base_url`.
