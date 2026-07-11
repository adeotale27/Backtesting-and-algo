# Disclaimer

**READ THIS BEFORE USING THE SOFTWARE.**

## Financial Risk

This software places **real orders with real money** on Indian stock
exchanges (NSE/BSE) through the Zerodha Kite Connect API when live trading is
enabled. Algorithmic trading of futures and options carries a **substantial
risk of loss**. You can lose more than your initial investment.

- Past performance of any strategy in this repository is **not** indicative
  of future results.
- The strategies shipped here were built for the author's personal use and
  risk profile. They are **not investment advice** and are not tuned for you.
- Bugs, network failures, exchange outages, API changes, and market
  conditions can all cause orders to be placed, modified, cancelled, or
  missed in ways you do not expect.

**You are solely responsible for every order this software places on your
account and for any resulting financial loss.**

## No Warranty

This software is provided **"AS IS"**, without warranty of any kind, express
or implied. The authors and contributors accept **no liability** for any
damages or losses — financial or otherwise — arising from its use. See the
[LICENSE](LICENSE) for the full warranty disclaimer.

## Not Affiliated with Zerodha

This is an independent open-source project. It is **not** affiliated with,
endorsed by, or supported by Zerodha Broking Ltd. or any exchange. "Zerodha",
"Kite", and "Kite Connect" are trademarks of their respective owners. Use of
the Kite Connect API is subject to Zerodha's own terms of service.

## Regulatory Notice

Automated/algorithmic trading may be subject to regulation by SEBI and your
broker's terms. It is **your responsibility** to ensure your use of this
software complies with all laws, regulations, and broker agreements that
apply to you.

## Safe Default

A fresh installation starts in **dry-run mode** (`[safety] live_trading =
false` in `configfile.ini`): every order is simulated and logged, nothing is
sent to the broker. Enable live trading only after you have read the code,
tested your configuration, and accepted the risks above.
