# FAQ

## Do I need a paid Zerodha API subscription?

Yes. Kite Connect (the official API) is a paid add-on from
[developers.kite.trade](https://developers.kite.trade). The free Kite web
login cannot be used by this app.

## Why does the header say DRY-RUN?

`[safety] live_trading = false` in `configfile.ini` — the safe default.
Every order-placement call is logged with full parameters but nothing is
sent to Zerodha. Set it to `true` and restart to trade for real (read the
[Disclaimer](disclaimer.md) first).

## Orders "fail" with return value -1 in dry-run — is that a bug?

No. In dry-run mode the order primitives log the simulated order and
return `-1` (the established "not placed" value), so strategy code follows
its no-fill path instead of tracking phantom orders.

## I log in every day — why?

Zerodha invalidates access tokens daily (around 6 AM IST). Click
**Connect Kite** in the header each trading morning to re-authenticate.
The token is cached so restarts within the day don't need a fresh login.

## Positions show no Greeks / everything is zero

Sync instruments first (⚡ menu → Sync Instruments). The instrument cache
must be populated before symbol→token lookups work. It auto-refreshes
daily at 9 AM IST afterwards.

## Can I run this on a VPS / cloud VM?

Yes — that's how the author runs it. Keep the app on `127.0.0.1`, put a
TLS reverse proxy in front, use strong gatekeeper credentials, and read
[Security](security.md) before opening any firewall port.

## Why is `pandas_ta` optional?

It needs Python ≥ 3.12. Without it, the expiry-trade Stochastic-RSI
signals are unavailable; everything else works.

## Some tests fail out of the box

A handful of tests are known-stale (they assert against older template
internals). CI tracks the current expected set; failures in
`test_theta_calc`/`test_trade_journal`/`test_watchdog` on a fresh clone are
known and unrelated to trading correctness.

## Where is the copy-trading / ML predictor / backtesting code?

This public repository is exported from a larger private codebase; those
modules are personal/experimental and intentionally not included.
