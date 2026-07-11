# Covered Calls

Dashboard: **`/covered_calls/`**

Sell OTM call options against equity you already hold, to earn premium:

- Reads your holdings and maps each stock to its F&O availability and lot
  size (holdings must cover at least one lot).
- Suggests OTM CE strikes with premium, annualized yield, and assignment
  price.
- Places the sell order (through `common_lib`, honoring dry-run mode) and
  tracks open covered-call positions against the underlying holding.
- Pending orders are tracked in the module's own SQLite store.
