# Configuration Reference

All configuration lives in `configfile.ini` next to `flask_app.py`. The
setup wizard writes it for you on first run; this page documents every key
for manual editing. **Restart the app after any change** — most values are
read once at startup.

## `[kite_login_details]`

| Key | Description |
|-----|-------------|
| `api_key` | Kite Connect app API key ([developers.kite.trade](https://developers.kite.trade)) |
| `api_secret` | Kite Connect app API secret |

Your Kite Connect app's **redirect URL** must be set to
`http://127.0.0.1:5010/login` (or your own base URL + `/login`).

## `[gatekeeper]`

| Key | Description |
|-----|-------------|
| `username` | Dashboard login username |
| `password` | Werkzeug password hash (recommended) or plaintext (works, logs a warning). Generate a hash with `python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpass'))"` |

Login is rate-limited: 5 failed attempts per IP → 15-minute lockout.

## `[safety]`

| Key | Default | Description |
|-----|---------|-------------|
| `live_trading` | `false` | **Dry-run kill switch.** `false` = every order-placement call is simulated and logged; nothing reaches Zerodha. `true` = real orders. If the key is absent entirely, live trading is assumed (legacy installs). |

## `[option_details]`

| Key | Description |
|-----|-------------|
| `current_volatility` | Fallback volatility (%) for Greeks when IV can't be derived |
| `interest_rate` | Risk-free interest rate (%) for Black-Scholes |
| `min_nifty_delta` / `max_nifty_delta` | NIFTY portfolio delta bounds enforced before hedging orders |
| `min_bank_nifty_delta` / `max_bank_nifty_delta` | Same for BANKNIFTY |
| `delta_calculation_days` | Business days to expiry used in delta calculations |

## `[others]`

| Key | Description |
|-----|-------------|
| `cool_off_time` | Seconds between consecutive orders (throttle) |
| `order_gtt_regular` | `regular` = limit orders; `gtt` = **`place_order()` transparently places GTTs instead** — fills then arrive via GTT triggers |

## `[notifications]`

| Key | Description |
|-----|-------------|
| `server_base_url` | Base URL used in notification links (default `http://127.0.0.1:5010`) |
| `telegram_bot_token` | Optional — BotFather token for Telegram alerts |
| `telegram_chat_id` | Optional — chat to send alerts to |
| VAPID keys | Optional — for browser Web Push |

## `[greeks]`

| Key | Default | Description |
|-----|---------|-------------|
| `library` | `mibian` | Greeks backend: `mibian` (embedded, pure Python) or `opengreeks` (Rust-backed, ~5–180× faster, `pip install opengreeks`) |
| `shadow_compare` | `false` | Compute both backends and log divergences (use before switching) |
| `shadow_tolerance` | `0.005` | Divergence tolerance for shadow comparison |

## State files (created at runtime, never commit)

| File | Purpose |
|------|---------|
| `instruments.db` | SQLite instrument cache, refreshed daily 9 AM IST |
| `.flask_secret` | Session-signing key (auto-generated) |
| `delta_limits.json` | Per-symbol delta trading limits |
| `../status/executed_orders_<date>.json` | Daily order history |
| `api_monitor.db` | Kite API call log |
| `trade_journal.db` | Trade journal cache |
