# Connector Hub (Threadless) — Hotfix
- 점검표 v1 정합 + DB_PATH ImportError 수정 포함.
- /ready → { ok, version, utc_now }
- /bridge/ingest → { ok, status, queued, dispatched, summary_sent, ... }
- 429 시뮬: 헤더 X-Debug-TG429:1 또는 env PUSH_SIMULATE_429=1


### Worker recovery
- Adds `yfinance` and a resilient watcher `market_watcher.py` with headers/backoff and HMAC push to the Hub.
