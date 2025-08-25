# Worker Stabilized Bundle (2025-08-25)

- Drop-in market watcher with resilient provider chain (AlphaVantage → yfinance → Yahoo).
- Headers/backoff for Yahoo 401/429.
- ENV: SENTINEL_BASE_URL (must be https URL), DATA_PROVIDERS, YF_ENABLED, ALPHAVANTAGE_API_KEY, WATCH_INTERVAL_SEC.

## Usage
Replace your worker file with `market_watcher.py` and add `requests`/`yfinance` to the image.

- Adds USE_PROXY_TICKERS env (default true): ES=F→SPY, NQ=F→QQQ when US market is closed.
