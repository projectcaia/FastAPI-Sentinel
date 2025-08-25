# Restore Pack — Market Watcher (2025-08-25)

## What this does
- Goes back to **legacy Sentinel mode**: POST to `/sentinel/alert` without HMAC.
- Sends **once every 30 minutes** (00/30), only **on level-change** by default.
- Optional: switch to hub mode later by setting `BRIDGE_MODE=hub` + `HUB_URL` + `CONNECTOR_SECRET`.

## Env (Railway Variables)
```
BRIDGE_MODE=sentinel
SENTINEL_URL=https://fastapi-sentinel-production.up.railway.app/sentinel/alert
WATCH_INTERVAL_SEC=1800
ALIGN_SLOTS=true
SEND_MODE=on_change
DATA_PROVIDERS=yfinance,yahoo
YF_ENABLED=true
USE_PROXY_TICKERS=true
# (Optional) ALPHAVANTAGE_API_KEY if you add 'alphavantage' to DATA_PROVIDERS
```

## Deploy
- Replace `market_watcher.py` in your worker image/repo with this file.
- Keep your Sentinel service **unchanged**.
- Redeploy worker → logs should show `mode=sentinel URL=.../sentinel/alert` and send at most once per slot.
