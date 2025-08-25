#!/usr/bin/env bash
set -euo pipefail
URL=${1:-http://localhost:8080}
BODY='{"idempotency_key":"BAD-001","source":"sentinel","type":"alert.market","priority":"high","timestamp":"2025-08-25T09:29:00+09:00","payload":{"rule":"iv_spike","index":"KOSPI200","level":"LV2"}}'
SIG=$(python - <<'PY'
import hmac,hashlib,os
body='''$BODY'''
secret=os.getenv('CONNECTOR_SECRET','sentinel_20250818_abcd1234')
print(hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest())
PY
)
curl -sS -X POST "$URL/bridge/ingest" -H 'Content-Type: application/json' -H "X-Signature: $SIG" -d "$BODY" | jq .
