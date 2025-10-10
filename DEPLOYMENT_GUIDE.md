# 센티넬 시스템 배포 가이드

## 🚀 Railway 배포 필수 환경변수

### 1. OpenAI & Assistant (필수)
```bash
OPENAI_API_KEY=sk-proj-...
CAIA_ASSISTANT_ID=asst_...
CAIA_THREAD_ID=thread_...  # 고정 스레드 사용 권장
```

### 2. Telegram 알림 (필수)
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### 3. Sentinel 설정
```bash
SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app
SENTINEL_KEY=your_secret_key  # 선택사항
LOG_LEVEL=INFO
```

### 4. DB증권 K200 선물 감시 (필수)
```bash
# DB증권 API 인증
DB_APP_KEY=your_db_app_key
DB_APP_SECRET=your_db_app_secret

# DB증권 모듈 활성화
DBSEC_ENABLE=true
DBSEC_USE_REST=true  # REST API 모드 (WebSocket 대신)

# 폴링 간격 설정 (초)
DB_POLL_INTERVAL_SEC=180  # 3분 간격 (기본값)

# 알림 임계값 (기존 시스템과 동일)
DB_ALERT_THRESHOLD=1.5  # LV2: 1.5%
DB_WARN_THRESHOLD=0.8   # LV1: 0.8%
```

### 5. Market Watcher 설정
```bash
# 감시 간격
WATCH_INTERVAL_SEC=180  # 3분 (DB증권과 동일)

# VIX 필터
VIX_FILTER_THRESHOLD=0.6  # 지수 0.6% 이상 변동시 VIX 감지

# 강제 시장 오픈 (휴장일에도 미국장 감시)
FORCE_MARKET_OPEN=true
```

---

## 📊 시스템 구조

### 센티넬 메인 (main.py)
- **알림 수신 엔드포인트**: `/sentinel/alert`
- **알림 조회**: `/sentinel/inbox`
- **DB증권 상태**: `/sentinel/dbsec/health`
- **Telegram 전송**: 모든 알림을 텔레그램으로 전송
- **Caia AI 통합**: Assistant API로 알림 분석

### DB증권 K200 선물 감시 (REST API)
- **주간거래**: 09:00 - 15:30 KST
- **야간거래**: 18:00 - 05:00 KST (다음날)
- **폴링 간격**: 3분 (180초)
- **알림 기준**:
  - LV1: ±0.8% 이상
  - LV2: ±1.5% 이상
  - LV3: ±2.5% 이상
- **중복 방지**: 동일 레벨 30분 내 중복 알림 차단

### Market Watcher (별도 워커)
- **한국 정규장** (09:00-15:30): KOSPI 현물
- **미국 정규장** (22:30-05:00): S&P 500, NASDAQ, VIX
- **선물 시장** (15:30-22:30): 미국 선물 (ES=F, NQ=F)
- **강제 모드**: 휴장일에도 미국 시장 감시

---

## 🎯 알림 형식 (개선)

### ✅ 지수 중심 알림 (NEW)
```json
{
  "index": "S&P 500",           // 메인: 지수명
  "level": "LV2",
  "delta_pct": -2.23,           // 지수 변동률
  "note": "LV2 진입 | VIX 21.1 (+28.5%)",  // VIX는 부가정보
  "kind": "US"
}
```

### ❌ 기존 VIX 중심 (OLD)
```json
{
  "index": "VIX",               // VIX가 메인
  "level": "LV2",
  "delta_pct": 28.48,          // VIX 변동률
  "note": "VIX LV2 진입",
  "kind": "VIX"
}
```

---

## 🔧 Railway 배포 순서

### 1. 환경변수 설정
Railway 프로젝트 > Variables 탭에서 위의 모든 환경변수 설정

### 2. Procfile 확인
```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

### 3. 배포 확인
```bash
# Health Check
curl https://your-project.railway.app/health

# DB증권 상태 확인
curl https://your-project.railway.app/sentinel/dbsec/health
```

### 4. 로그 모니터링
Railway 대시보드에서 로그 확인:
- `[DBSEC] K200 선물지수 monitoring services...`
- `[DBSEC] Starting K200 선물지수 polling (interval: 3분)`
- `[DBSEC] K200 선물: 350.25 (+0.85%) Vol: ...`

---

## 📝 알림 테스트

### 1. 수동 테스트 알림 전송
```bash
curl -X POST https://your-project.railway.app/sentinel/alert \
  -H "Content-Type: application/json" \
  -H "x-sentinel-key: your_key" \
  -d '{
    "index": "TEST",
    "level": "LV1",
    "delta_pct": 1.5,
    "triggered_at": "2025-10-11T04:00:00+09:00",
    "note": "테스트 알림"
  }'
```

### 2. DB증권 테스트 알림
```bash
curl -X POST https://your-project.railway.app/sentinel/dbsec/alert/test
```

### 3. 실제 알림 예시
#### K200 선물 알림
```
📡 [LV2] K200 선물 +1.52% / ⏱ 2025-10-11T13:45:23+09:00
📝 K200 선물 상승 1.52% (DB증권)
```

#### 미국 지수 알림 (VIX 포함)
```
📡 [LV2] S&P 500 -2.23% / ⏱ 2025-10-11T04:29:30+09:00
📝 LV2 진입 | VIX 21.1 (+28.5%)
```

---

## 🚨 문제 해결

### DB증권 토큰 오류
```bash
# 수동 토큰 갱신
curl -X POST https://your-project.railway.app/sentinel/dbsec/token/refresh
```

### 알림이 오지 않음
1. **환경변수 확인**: `DB_APP_KEY`, `DB_APP_SECRET` 설정 확인
2. **거래 시간 확인**: 주간(09:00-15:30) 또는 야간(18:00-05:00) 시간대인지 확인
3. **변동률 확인**: 0.8% 이상 변동해야 알림 발생
4. **중복 방지**: 동일 레벨 30분 내 중복 알림 차단

### WebSocket 타임아웃
- `DBSEC_USE_REST=true` 설정하여 REST API 모드 사용 (권장)

### K200 선물 데이터 없음
1. **거래 시간 확인**: 주간/야간 거래 시간대인지 확인
2. **API 로그 확인**: Railway 로그에서 `[DBSEC]` 라인 확인
3. **선물 코드 확인**: 기본값 `101C6000` (현재 월물)

---

## 📊 모니터링 대시보드

### Railway 로그 필터
- `[DBSEC]` - DB증권 K200 선물 관련
- `[market-watcher]` - Market Watcher 일반 감시
- `[CAIA]` - AI Assistant 호출
- `>>> 알림 전송` - 실제 알림 발송

### 주요 로그 메시지
```
✅ 정상 작동:
[DBSEC] K200 선물: 350.25 (+0.85%) Vol: 123,456
[DBSEC] Alert sent: K200 선물 +0.85% Level LV1

⚠️ 경고:
[DBSEC] Failed to get price data (3/5)
[DBSEC] API request failed: 401

❌ 오류:
[DBSEC] Token manager not available
[DBSEC] Failed to initialize DB증권 services
```

---

## 🎯 성능 최적화

### 폴링 간격 조정
- **빠른 감지**: `DB_POLL_INTERVAL_SEC=60` (1분)
- **표준**: `DB_POLL_INTERVAL_SEC=180` (3분, 권장)
- **절약 모드**: `DB_POLL_INTERVAL_SEC=300` (5분)

### 중복 알림 제어
- **짧게**: `alert_cooldown_minutes=15` (15분)
- **표준**: `alert_cooldown_minutes=30` (30분, 기본값)
- **길게**: `alert_cooldown_minutes=60` (1시간)

---

## 📞 지원

문제가 발생하면:
1. Railway 로그 확인
2. Health Check 엔드포인트 호출
3. 환경변수 재확인
4. 수동 토큰 갱신 시도

---

**시스템 버전**: v2.0.0 (2025-10-11)  
**업데이트**: K200 선물 감시 추가, 알림 형식 개선, 지수 중심 알림
