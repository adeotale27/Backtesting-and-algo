# Disclaimer

!!! danger "You are responsible for every order this software places"

This software places **real orders with real money** on Indian stock
exchanges (NSE/BSE) through the Zerodha Kite Connect API when live trading
is enabled.

## Financial risk

- Algorithmic F&O trading carries a **substantial risk of loss** — you can
  lose more than your initial investment.
- The strategies here were built for the author's personal use and risk
  profile. They are **not investment advice**.
- Bugs, network failures, exchange outages, API changes, and market
  conditions can cause orders to be placed, modified, cancelled, or missed
  in ways you do not expect.
- Past performance is not indicative of future results.

## No warranty, no liability

This software is provided **"AS IS"**, without warranty of any kind. The
authors and contributors accept **no liability for any damages or losses —
financial or otherwise** — arising from its use. See the repository
`LICENSE` (MIT) for the formal terms.

## Not affiliated with Zerodha

This is an independent open-source project — not affiliated with, endorsed
by, or supported by Zerodha Broking Ltd. Use of the Kite Connect API is
subject to Zerodha's own terms.

## Regulatory

Automated trading may be subject to SEBI regulation and your broker's
terms. Compliance is **your responsibility**.

## The safe default

Fresh installs run with `[safety] live_trading = false`: every order is
simulated and logged; nothing reaches the broker. Keep it that way until
you have read the code, tested your configuration, and accepted these risks.
