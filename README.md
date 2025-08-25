# Connector Hub (Threadless) — Patched

FastAPI 기반 **센티넬 알람 허브** — `/bridge/ingest`로 알람 수신 → **HMAC 검증** → **멱등성(SQLite)** → **텔레그램 푸시**.
- **배포 점검표 v1 정합성 패치 포함**: `/ready`는 `utc_now`(UTC ISO), `/bridge/ingest` 응답에 `queued/dispatched/summary_sent` 추가, 환경변수 `PUSH_SIMULATE_429` 지원.

## Quickstart
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # 토큰/시크릿 설정
./run_local.sh
```

### Health
- `GET /ready` → 200 OK + `{ ok, version, utc_now }`
- `GET /health` → DB/오류 카운트
- `GET /jobs?hours=24&limit=50` → 최근 작업

## HMAC
- 헤더 `X-Signature = HMAC_SHA256(raw_body, CONNECTOR_SECRET)`
- 불일치 → 401

## 멱등성
- 헤더 `Idempotency-Key` 또는 JSON `idempotency_key`
- 동일 키 2회째 `{ "dedup": true }`

## 푸시 포맷
```
[Sentinel/LV2] KOSPI200 iv_spike
rule=iv_spike index=KOSPI200 level=LV2 priority=high
metrics: ΔK200 1.6%, ΔVIX 7.2%
job: https://hub/jobs/<idempotency_key>
ACK: SNT-20250825-0929-AB12
Copy for Caia: 센티넬 반영 SNT-20250825-0929-AB12
```

## 429 시뮬레이션
- 요청 헤더 `X-Debug-TG429: 1` **또는** 환경변수 `PUSH_SIMULATE_429=1`

## 배포
- Docker: `docker build -t hub . && docker run -p 8080:8080 --env-file .env hub`
- PM2: `pm2 start ecosystem.config.js`

## 트러블슈팅
- 401/422/429/500 케이스는 `events` 테이블과 서버 로그(JSON) 확인
