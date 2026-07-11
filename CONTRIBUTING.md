# Contributing

Thanks for your interest! This project welcomes bug reports, fixes, docs
improvements, and well-scoped features.

## Development setup

```bash
git clone https://github.com/Raahi-Bhushan/ui-trading-system.git
cd ui-trading-system
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install pytest pre-commit
pre-commit install        # gitleaks secret scan on every commit
pytest tests/
```

Run the app with `python flask_app.py` — the setup wizard at `/setup` writes
your `configfile.ini`. Keep `[safety] live_trading = false` while developing.

## Ground rules

- **Never commit secrets.** No API keys, tokens, `configfile.ini`, `.db`
  files, or logs. The pre-commit gitleaks hook and CI both scan for leaks.
- **Dry-run first.** Any change to order-placement paths must be exercised in
  dry-run mode and covered by tests before a PR.
- **Tests use real SQLite.** Do not mock the database layer — a
  mocked-test/production divergence has bitten this project before. Create a
  temp DB in the test instead.
- **Retry logic.** Wrap Kite API calls that can hit rate limits with
  `retry_with_backoff()` from `common_lib`.
- **Instrument tokens.** Always resolve via
  `instrument_cache.get_instrument_token()` — tokens change after corporate
  actions. Never hardcode token integers or lot sizes.
- **IST everywhere.** Use `get_ist_now()` from `common_lib`, never
  `datetime.now()` — servers may run in UTC.
- **Greeks.** Go through `greeks_lib` (`import greeks_lib as mibian`); never
  import the embedded `mibian` package directly.

## Code style

- Python 3.11+, PEP 8, Black-compatible formatting.
- Type hints on new/changed function signatures.
- Google-style docstrings (`Args:` / `Returns:` / `Raises:`).
- Use `logging`, never `print()`, in application code.

## About this repository's history

This public repository is exported from a larger private codebase; some
experimental modules are intentionally absent, and history starts from the
initial public release. If a change you need touches something that appears
"missing", open an issue and we'll figure out the right seam.

## Pull requests

1. Fork, create a topic branch, keep the diff focused.
2. `pytest tests/` must pass; CI also runs a secret scan.
3. Describe **what** and **why**; link the issue if one exists.
