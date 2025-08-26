# Sentinel Hub Patch (clean)
Add two lines to your `main.py`:

```python
from hub_patch import register_hub_forwarder
register_hub_forwarder(app)
```

Place `hub_patch.py` at `app/hub_patch.py`.

Env:
```
HUB_URL=https://<HUB_DOMAIN>.up.railway.app/bridge/ingest
CONNECTOR_SECRET=sentinel_20250818_abcd1234
```
