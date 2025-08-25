#!/usr/bin/env bash
set -euo pipefail
URL=${1:-http://localhost:8080}
curl -sS "$URL/jobs?hours=24&limit=50" | jq .
