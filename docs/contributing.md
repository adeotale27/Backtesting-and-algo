# Contributing

See the repository's
[CONTRIBUTING.md](https://github.com/Raahi-Bhushan/ui-trading-system/blob/main/CONTRIBUTING.md)
for the full guide. The short version:

- `pip install -r requirements.txt`, `pip install pytest pre-commit`,
  `pre-commit install`, `pytest tests/`.
- Never commit secrets; keep `[safety] live_trading = false` while
  developing.
- Tests use real SQLite — don't mock the DB layer.
- Use `retry_with_backoff()` for Kite calls, `get_ist_now()` for time,
  `instrument_cache` for token lookups, `greeks_lib` for options math.
