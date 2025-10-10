# 🚀 Railway 빠른 배포 가이드

## 📋 개요

Sentinel 시스템은 두 개의 서비스로 구성됩니다:

1. **Sentinel** (메인 서버) - FastAPI 웹 서버, 알림 처리
2. **Sentinel-Worker** (감시자) - 시장 감시 워커

---

## 🔧 Railway 환경 변수 설정

### 1️⃣ Sentinel (메인 서버)

**필수 변수 4개만:**

```bash
# 로그 레벨
LOG_LEVEL=INFO

# DB증권 라우터 비활성화 (중복 방지)
DBSEC_ROUTER_ENABLE=false

# 텔레그램 봇
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 2️⃣ Sentinel-Worker (감시자)

**필수 변수 12개:**

```bash
# === 기본 설정 ===
LOG_LEVEL=INFO
FORCE_MARKET_OPEN=true
WATCH_INTERVAL_SEC=180

# === VIX 필터 ===
VIX_FILTER_THRESHOLD=0.8

# === K200 선물 (DB증권 API) ===
DBSEC_ENABLE=true
K200_CHECK_INTERVAL_MIN=30
DB_FUTURES_CODE=101RB000
DB_API_BASE=https://openapi.dbsec.co.kr:8443
DB_APP_KEY=your_db_app_key
DB_APP_SECRET=your_db_app_secret

# === Sentinel 연결 ===
SENTINEL_BASE_URL=https://your-sentinel-url.railway.app
SENTINEL_KEY=your_sentinel_auth_key
```

---

## 🎯 중요 변수 설명

### 🔴 **가장 중요: FORCE_MARKET_OPEN**

```bash
FORCE_MARKET_OPEN=true  # ← 반드시 true로 설정!
```

**이유:**
- 야간 선물 감시(18:00-05:00)를 위해 **필수**
- `false`로 설정하면 휴장 시간에 감시 중단 → 야간 선물 놓침

### ⏱️ **WATCH_INTERVAL_SEC**

```bash
WATCH_INTERVAL_SEC=180  # 3분 (권장)
```

**이유:**
- 3분 주기로 실시간 변동성 감지
- `1800` (30분)은 너무 느림

### 📊 **K200_CHECK_INTERVAL_MIN**

```bash
K200_CHECK_INTERVAL_MIN=30  # 30분 (권장)
```

**이유:**
- K200 선물은 30분마다 별도 체크
- DB증권 API 호출 비용 관리

### 🔑 **DB_FUTURES_CODE**

```bash
DB_FUTURES_CODE=101RB000  # 2025년 12월물
```

**주의:**
- **분기별 업데이트 필요!**
- 최근월물 만료 전에 다음 월물로 변경

---

## 📝 Railway 설정 단계

### Step 1: Sentinel 서비스 생성

```bash
# Railway 대시보드에서
New → Deploy from GitHub → projectcaia/FastAPI-Sentinel
Service Name: sentinel
Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
```

**환경 변수 추가:**
```bash
LOG_LEVEL=INFO
DBSEC_ROUTER_ENABLE=false
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Step 2: Sentinel-Worker 서비스 생성

```bash
# Railway 대시보드에서
New → Deploy from GitHub → projectcaia/FastAPI-Sentinel
Service Name: sentinel-worker
Start Command: python market_watcher.py
```

**환경 변수 추가:**
```bash
# 복사해서 붙여넣기
LOG_LEVEL=INFO
FORCE_MARKET_OPEN=true
WATCH_INTERVAL_SEC=180
VIX_FILTER_THRESHOLD=0.8
DBSEC_ENABLE=true
K200_CHECK_INTERVAL_MIN=30
DB_FUTURES_CODE=101RB000
DB_API_BASE=https://openapi.dbsec.co.kr:8443
DB_APP_KEY=...
DB_APP_SECRET=...
SENTINEL_BASE_URL=https://sentinel-production-xxxx.railway.app
SENTINEL_KEY=...
```

### Step 3: Sentinel URL 연결

1. Sentinel 서비스가 배포되면 **Public URL** 복사
   - 예: `https://sentinel-production-1234.railway.app`

2. Sentinel-Worker 환경 변수에 추가:
   ```bash
   SENTINEL_BASE_URL=https://sentinel-production-1234.railway.app
   ```

3. Sentinel-Worker 재시작

---

## ✅ 배포 확인

### 1. Sentinel 서비스 확인

**URL 접속:**
```
https://your-sentinel-url.railway.app/health
```

**예상 응답:**
```json
{
  "status": "ok",
  "service": "Sentinel",
  "timestamp": "2025-10-10T12:00:00+09:00"
}
```

### 2. Sentinel-Worker 로그 확인

**Railway 대시보드:**
```
Sentinel-Worker → Logs
```

**예상 로그:**
```
2025-10-10 12:00:00 - ==========================================
2025-10-10 12:00:00 - 시장 체크 시작 [세션: US] 2025-10-10 12:00:00 KST
2025-10-10 12:00:00 - 🔴 강제 시장 오픈 모드 활성화 - 휴장일에도 감시 계속
2025-10-10 12:00:00 - 📊 K200 선물 감시 활성화 (DB증권 API)
2025-10-10 12:00:00 - ==========================================
```

### 3. 야간 선물 감시 확인 (18:00-05:00)

**예상 로그 (야간 시간대):**
```
2025-10-10 19:30:00 - 📊 K200 선물 체크 시작...
2025-10-10 19:30:01 - ✓ K200 선물: 현재=341.50, 변화=-1.23%
2025-10-10 19:30:01 - >>> K200 선물 알림: [LV2] K200 선물 하락 1.23% (DB증권 API)
```

---

## 🐛 문제 해결

### ❌ "Sentinel URL 연결 실패" 에러

**원인:**
- `SENTINEL_BASE_URL` 잘못 설정
- Sentinel 서비스가 아직 시작 안 됨

**해결:**
```bash
# 1. Sentinel 서비스 Public URL 확인
Railway → Sentinel → Settings → Public Networking

# 2. Sentinel-Worker 환경 변수 업데이트
SENTINEL_BASE_URL=https://correct-url.railway.app

# 3. Sentinel-Worker 재시작
```

### ❌ "DB증권 토큰 발급 실패" 에러

**원인:**
- API 키 만료
- API 키 오타

**해결:**
```bash
# 1. DB증권 사이트에서 새 API 키 발급
# 2. Railway 환경 변수 업데이트
DB_APP_KEY=new_key_here
DB_APP_SECRET=new_secret_here

# 3. Sentinel-Worker 재시작
```

### ❌ 야간에 K200 알림이 안 와요

**체크리스트:**
```bash
# 1. FORCE_MARKET_OPEN 확인
✅ FORCE_MARKET_OPEN=true

# 2. DBSEC_ENABLE 확인
✅ DBSEC_ENABLE=true

# 3. 시간 확인 (18:00-05:00 KST)
✅ 현재 시간이 야간 거래 시간인지 확인

# 4. 로그 확인
Railway → Sentinel-Worker → Logs → 검색: "K200"
```

---

## 📊 시간대별 감시 계획

| 시간 (KST) | 세션 | 감시 대상 |
|-----------|------|----------|
| 09:00-15:30 | KR | KOSPI + K200 선물 (주간) |
| 15:30-18:00 | CLOSED | 대기 |
| 18:00-22:30 | CLOSED/US | **K200 선물 (야간)** ← 중요! |
| 22:30-05:00 | US | S&P 500, NASDAQ, VIX + **K200 선물 (야간)** |
| 05:00-09:00 | CLOSED | 대기 |

**주의:**
- 주말/공휴일 제외
- **18:00-05:00 야간 선물 감시가 가장 중요!**

---

## 🎉 완료!

### 배포 체크리스트

- [x] Sentinel 서비스 생성 및 환경 변수 설정
- [x] Sentinel-Worker 서비스 생성 및 환경 변수 설정
- [x] `FORCE_MARKET_OPEN=true` 확인
- [x] `SENTINEL_BASE_URL` 올바른 URL로 설정
- [x] DB증권 API 키 설정
- [x] 두 서비스 모두 정상 실행 확인
- [x] 로그에서 "시장 체크 시작" 메시지 확인
- [x] 텔레그램 봇 알림 수신 확인

### 다음 단계

1. **야간 시간대(18:00-05:00) 모니터링**
   - Railway 로그 확인
   - K200 선물 체크 메시지 확인
   - 텔레그램 알림 수신 확인

2. **정기 점검**
   - DB증권 API 키 유효기간 확인
   - K200 선물 종목 코드 분기별 업데이트
   - 로그에서 에러 메시지 체크

3. **최적화**
   - 알림 임계값 조정 (필요 시)
   - 감시 주기 조정 (필요 시)
   - VIX 필터 임계값 조정 (필요 시)

---

## 📚 추가 문서

- [ENV_VARS_GUIDE.md](./ENV_VARS_GUIDE.md) - 환경 변수 상세 가이드
- [NIGHT_FUTURES_FIX.md](./NIGHT_FUTURES_FIX.md) - 야간 선물 수정 내역
- [FINAL_DEPLOYMENT.md](./FINAL_DEPLOYMENT.md) - 전체 배포 가이드

---

**작성일**: 2025-10-10  
**버전**: 1.0
