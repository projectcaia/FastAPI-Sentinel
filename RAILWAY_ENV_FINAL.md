# Railway 환경변수 최종 설정

## 🔴 필수 환경변수

### 1. OpenAI & Assistant
```bash
OPENAI_API_KEY=your_openai_api_key
CAIA_ASSISTANT_ID=asst_BZDtN...  # Caia Assistant ID
```

### 2. Telegram
```bash
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

### 3. DB증권 API (KODEX 200 ETF 감시)
```bash
DB_APP_KEY=your_db_app_key
DB_APP_SECRET=your_db_app_secret
# KODEX 200 ETF(069500)를 통해 K200 지수 추적
```

### 4. Sentinel 설정
```bash
SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app
SENTINEL_KEY=your_sentinel_key  # 없어도 작동
```

## 🟢 권장 설정

```bash
# DB증권 설정 (REST API 모드 권장)
DBSEC_ENABLE=true               # DB증권 모듈 활성화
DBSEC_USE_REST=true             # REST API 사용 (WebSocket 대신)
DB_POLL_INTERVAL_SEC=180        # 3분마다 가격 조회

# 알림 임계값 (다른 지표와 동일)
DB_ALERT_THRESHOLD=1.5          # LV2: 1.5% 이상
DB_WARN_THRESHOLD=0.8           # LV1: 0.8% 이상

# 로그 레벨
LOG_LEVEL=INFO                  # INFO 권장 (DEBUG는 너무 많은 로그)

# Market Watcher (별도 워커)
WATCH_INTERVAL_SEC=300          # 5분마다 시장 감시
VIX_FILTER_THRESHOLD=0.8        # VIX 필터 임계값
```

## 🔵 선택 설정

```bash
# Hub 연동 (선택)
HUB_URL=your_hub_url
CONNECTOR_SECRET=your_connector_secret

# 고정 Thread (선택)
CAIA_THREAD_ID=thread_xxx       # 고정 스레드 사용 시
```

## 📝 작동 확인

### 1. 루트 확인
```bash
curl https://fastapi-sentinel-production.up.railway.app/
# {"service":"Sentinel FastAPI v2","status":"operational"...}
```

### 2. Health Check
```bash
curl https://fastapi-sentinel-production.up.railway.app/health
# {"status":"ok","version":"sentinel-fastapi-v2-1.4.1-patched"...}
```

### 3. DB증권 상태
```bash
curl https://fastapi-sentinel-production.up.railway.app/sentinel/dbsec/health
# {"status":"healthy","token_manager":{...},"futures_monitor":{...}}
```

### 4. 테스트 알림
```bash
curl -X POST https://fastapi-sentinel-production.up.railway.app/sentinel/dbsec/alert/test
```

## 🚨 중요 사항

1. **REST API 모드 사용**: `DBSEC_USE_REST=true`로 설정하여 WebSocket 대신 REST API 사용
2. **3분 폴링**: 너무 자주 호출하면 API 한도 초과 가능
3. **로그 레벨**: `LOG_LEVEL=INFO`로 설정하여 불필요한 로그 제거
4. **Market Watcher**: 별도 워커로 실행되며 TradingView 크롤링

## 🔧 문제 해결

### WebSocket 타임아웃
→ `DBSEC_USE_REST=true` 설정하여 REST API 모드 사용

### 알림이 오지 않음
→ K200 선물이 0.8% 이상 변동해야 알림 발생
→ 거래시간 확인 (주간: 09:00-15:30, 야간: 18:00-05:00)

### 토큰 오류
→ DB_APP_KEY와 DB_APP_SECRET 확인
→ `/sentinel/dbsec/token/refresh`로 토큰 갱신

## 📊 시스템 구조

```
FastAPI (main.py)
├── /sentinel/alert     → 알림 수신 엔드포인트
├── /sentinel/inbox     → 알림 조회
└── /sentinel/dbsec/*   → DB증권 K200 선물 감시

Market Watcher (별도 워커)
└── TradingView 크롤링 → /sentinel/alert로 전송

DB증권 REST Poller
└── 3분마다 K200 선물 가격 조회 → 변동 감지 → /sentinel/alert
```