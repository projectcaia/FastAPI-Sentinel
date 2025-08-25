#!/usr/bin/env bash
set -euo pipefail
URL=${1:-http://localhost:8080}
BODY='{"idempotency_key":"DUP-001","source":"sentinel","type":"alert.market","priority":"high","timestamp":"2025-08-25T09:29:00+09:00","payload":{"rule":"iv_spike","index":"KOSPI200","level":"LV2","metrics":{"dK200":1.6,"dVIX":7.2}}}'
curl -sS -X POST "$URL/bridge/ingest" -H 'Content-Type: application/json' -H "X-Signature: deadbeef" -d "$BODY" | jq .
