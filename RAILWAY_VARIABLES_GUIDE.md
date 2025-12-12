# Railway 환경변수 설정 가이드

## ⚠️ 중요: 이 파일은 Git에 커밋하지 마세요!

이 가이드는 Railway Dashboard에서 직접 환경변수를 설정하는 방법을 안내합니다.

---

## 📋 Sentinel (Main API) 환경변수

**Railway Dashboard → FastAPI-Sentinel → Variables 탭**

### 현재 설정된 값 확인 후 아래 변수들을 추가/수정:

```bash
# === OpenAI & Caia ===
OPENAI_API_KEY=<현재 Railway에 설정된 값 유지>
CAIA_ASSISTANT_ID=<현재 Railway에 설정된 값 유지>
CAIA_THREAD_ID=<현재 Railway에 설정된 값 유지>
CAIA_PUSH_MODE=telegram

# === Telegram ===
TELEGRAM_BOT_TOKEN=<현재 Railway에 설정된 값 유지>
TELEGRAM_CHAT_ID=<현재 Railway에 설정된 값 유지>

# === Hub Connector ===
HUB_URL=<현재 Railway에 설정된 값 유지>
CONNECTOR_SECRET=<현재 Railway에 설정된 값 유지>

# === Sentinel API ===
SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app

# === 시장 감시 설정 ===
WATCH_INTERVAL_SEC=1800
WATCHER_STATE_PATH=./market_state.json
DEDUP_WINDOW_MIN=30
USE_PROXY_TICKERS=true

# === 볼린저 밴드 ===
BOLL_K_SIGMA=2.0
BOLL_WINDOW=20

# === DB증권 API ===
DB_API_BASE=https://openapi.dbsec.co.kr:8443
DB_APP_KEY=<현재 Railway에 설정된 값 유지>
DB_APP_SECRET=<현재 Railway에 설정된 값 유지>
DB_SCOPE=oob
DBSEC_ROUTER_ENABLE=false

# === 로깅 ===
LOG_LEVEL=INFO
```

---

## 📋 Sentinel Worker (Cron Job) 환경변수

**Railway Dashboard → Sentinel Worker → Variables 탭**

### 위의 Sentinel Main API 변수 모두 + 아래 추가:

```bash
# === 위의 모든 Main API 변수 포함 ===

# === Worker 전용 추가 변수 ===
DATA_PROVIDERS=alphavantage,yfinance,yahoo
ALPHAVANTAGE_API_KEY=<현재 Railway에 설정된 값 유지>
YF_ENABLED=true
SEND_MODE=on_change
BRIDGE_MODE=hub
ALIGN_SLOTS=true

# === DB증권 Worker 설정 ===
DBSEC_ENABLE=true
DB_FUTURES_CODE=101C6000
K200_CHECK_INTERVAL_MIN=30
FORCE_MARKET_OPEN=false

# === VIX 필터 ===
VIX_FILTER_THRESHOLD=0.6
```

---

## 🔧 설정 방법

### 1. Railway Dashboard 접속
```
https://railway.app/dashboard
```

### 2. 프로젝트 선택
- Sentinel (Main API) 또는 Sentinel Worker

### 3. Variables 탭 이동
- 좌측 메뉴에서 "Variables" 클릭

### 4. 변수 추가/수정
- "Add Variable" 버튼 클릭
- Key/Value 입력
- 또는 "Raw Editor" 사용하여 일괄 입력

### 5. 배포 자동 시작
- 변수 저장 시 자동으로 재배포 시작

---

## 📝 주요 변수 설명

### DBSEC_ROUTER_ENABLE
- **Main API**: `false` (DB증권 라우터 비활성화)
- **Worker**: 설정 불필요 (DBSEC_ENABLE만 사용)

### DBSEC_ENABLE
- **Main API**: 설정 불필요
- **Worker**: `true` (DB증권 API 활성화)

### DB_FUTURES_CODE
- **현재**: `101C6000` (2025년 12월물)
- **분기별 업데이트 필요**:
  - 2026년 3월물: `101RC000`
  - 2026년 6월물: `101SC000`
  - 2026년 9월물: `101UC000`

### CAIA_PUSH_MODE
- `telegram`: Telegram으로 알림 전송 (현재 설정)
- Actions GPT는 Railway 도메인 통해 자동 연결

---

## ✅ 설정 확인 방법

### 1. Health Check
```bash
curl https://fastapi-sentinel-production.up.railway.app/health
```

### 2. 로그 확인
Railway Dashboard → Deployments → Logs

### 3. 변수 확인
Railway Dashboard → Variables → 모든 변수 목록 확인

---

## 🚨 트러블슈팅

### 변수가 적용되지 않음
1. Railway Dashboard에서 변수 저장 확인
2. 재배포 시작 확인 (Deployments 탭)
3. 로그에서 환경변수 로드 메시지 확인

### 배포 실패
1. 로그에서 에러 메시지 확인
2. 변수명 오타 확인
3. 변수값 형식 확인 (따옴표 불필요)

### API 키 오류
1. Railway Variables에서 키 값 재확인
2. 키 앞뒤 공백 제거
3. 키 만료 여부 확인

---

## 📌 참고사항

- **보안**: 이 파일은 Git에 커밋하지 마세요!
- **키 관리**: 실제 키 값은 Railway Dashboard에서만 관리
- **백업**: Railway Variables 스크린샷 저장 권장
- **변경 이력**: Railway에서 변수 변경 이력 자동 저장

---

## 🔗 관련 문서

- `DEPLOYMENT_GUIDE.md`: 전체 배포 가이드
- `CRON_SETUP_GUIDE.md`: Cron Job 설정 가이드
- `.env.example`: 환경변수 템플릿
- `.env.worker.example`: Worker 환경변수 템플릿
