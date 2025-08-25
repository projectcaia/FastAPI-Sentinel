#!/usr/bin/env bash
set -Eeuo pipefail
[ -f ".env" ] && source .env
export PYTHONUNBUFFERED=1
export PORT=${PORT:-8080}
mkdir -p data
python -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT}" --reload
