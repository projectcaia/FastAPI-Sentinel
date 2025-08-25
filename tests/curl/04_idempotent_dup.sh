#!/usr/bin/env bash
set -euo pipefail
URL=${1:-http://localhost:8080}
IDK="SAME-KEY-001"
BODY=$(cat <<JSON
{
  "idempotency_key":"$IDK",
  "source":"sentinel",
  "type":"alert.market",
  "priority":"high",
  "timestamp":"2025-08-25T09:29:00+09:00",
  "payload":{"rule":"iv_spike","index":"KOSPI200","level":"LV2","metrics":{"dK200":1.6,"dVIX":7.2}}
}
JSON
)
SIG=$(python - <<'PY'
import hmac,hashlib,os
body=open(0).read()
secret=os.getenv('CONNECTOR_SECRET','sentinel_20250818_abcd1234')
print(hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest())
PY
<<<"$BODY")
# First
curl -sS -X POST "$URL/bridge/ingest" -H 'Content-Type: application/json' -H "Idempotency-Key: $IDK" -H "X-Signature: $SIG" -d "$BODY" | jq .
# Second (dedup)
curl -sS -X POST "$URL/bridge/ingest" -H 'Content-Type: application/json' -H "Idempotency-Key: $IDK" -H "X-Signature: $SIG" -d "$BODY" | jq .
