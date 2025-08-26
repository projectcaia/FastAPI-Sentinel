# hub_patch — Sentinel → Connector Hub Forwarder (2-line integration)

## What it does
- Intercepts `POST /sentinel/alert` on your Sentinel FastAPI app
- Lets your existing handler run first (Telegram etc.)
- If the response is 200, forwards the same alert to Connector Hub `/bridge/ingest`
- HMAC and Idempotency-Key are added automatically

## Install
1) Drop `hub_patch.py` next to your FastAPI app code (PYTHONPATH-visible).
2) Add **two lines** near where you create your `FastAPI()` app:
```python
from hub_patch import register_hub_forwarder
register_hub_forwarder(app)
```
3) Set environment variables:
```
HUB_URL=https://<HUB_DOMAIN>.up.railway.app/bridge/ingest
CONNECTOR_SECRET=sentinel_20250818_abcd1234
```
4) Deploy and test:
- Trigger a normal alert (Telegram should still work)
- Check your Hub logs for `POST /bridge/ingest 200`
- `curl -sS https://<HUB_DOMAIN>.up.railway.app/jobs | jq .`

## Rollback
- Remove the two lines; delete `hub_patch.py` (no other changes needed).
