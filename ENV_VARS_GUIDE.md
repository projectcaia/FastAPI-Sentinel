# 🔧 Sentinel 환경 변수 가이드

## 📋 목차
1. [핵심 필수 변수](#핵심-필수-변수)
2. [Sentinel (메인 서버) 전용](#sentinel-메인-서버-전용)
3. [Sentinel-Worker (감시자) 전용](#sentinel-worker-감시자-전용)
4. [권장 설정값](#권장-설정값)
5. [문제 해결](#문제-해결)

---

## 핵심 필수 변수

### 🚨 양쪽 모두 필요한 공통 변수

```bash
# 텔레그램 봇 (알림 전송)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Sentinel 연결 정보
SENTINEL_BASE_URL=https://your-sentinel-url.railway.app
SENTINEL_KEY=your_sentinel_auth_key
```

---

## Sentinel (메인 서버) 전용

### 최소 필수 변수 (4개만)

```bash
# 1. 로그 레벨
LOG_LEVEL=INFO

# 2. DB증권 라우터 비활성화 (중요!)
DBSEC_ROUTER_ENABLE=false

# 3. 텔레그램 설정 (위 공통 변수 참조)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### 설명
- **LOG_LEVEL**: `DEBUG`, `INFO`, `WARNING`, `ERROR` 중 선택
- **DBSEC_ROUTER_ENABLE**: **반드시 `false`로 설정** (Market Watcher가 DB증권 API 직접 호출, 중복 방지)

---

## Sentinel-Worker (감시자) 전용

### 핵심 필수 변수 (12개)

```bash
# ==================== 기본 설정 ====================
# 1. 로그 레벨
LOG_LEVEL=INFO

# 2. 강제 시장 오픈 (야간 선물 감시에 필수!)
FORCE_MARKET_OPEN=true

# 3. 감시 주기 (초 단위)
WATCH_INTERVAL_SEC=180

# ==================== VIX 필터 설정 ====================
# 4. VIX 알림 필터링 임계값
VIX_FILTER_THRESHOLD=0.8

# ==================== K200 선물 설정 (DB증권 API) ====================
# 5. DB증권 API 활성화
DBSEC_ENABLE=true

# 6. K200 선물 체크 주기 (분 단위)
K200_CHECK_INTERVAL_MIN=30

# 7. K200 선물 종목 코드
DB_FUTURES_CODE=101RB000

# 8. DB증권 API Base URL
DB_API_BASE=https://openapi.dbsec.co.kr:8443

# 9. DB증권 앱 키
DB_APP_KEY=your_db_app_key_here

# 10. DB증권 앱 시크릿
DB_APP_SECRET=your_db_app_secret_here

# ==================== Sentinel 연결 ====================
# 11-12. Sentinel 메인 서버 정보
SENTINEL_BASE_URL=https://your-sentinel-url.railway.app
SENTINEL_KEY=your_sentinel_auth_key
```

### 주요 변수 설명

#### 🔴 **FORCE_MARKET_OPEN** (가장 중요!)
```bash
FORCE_MARKET_OPEN=true  # ✅ 야간 선물 감시를 위해 반드시 true!
```
- **`true`**: 시장 휴장 시간에도 감시 계속 (야간 선물 18:00-05:00 필수)
- **`false`**: 휴장 시간에는 감시 중단 (❌ 야간 선물 감시 불가)

#### ⏱️ **WATCH_INTERVAL_SEC**
```bash
WATCH_INTERVAL_SEC=180  # 3분 권장
```
- 감시 주기 (초 단위)
- **권장값**: `180` (3분) - 실시간 변동성 감지
- **비권장**: `1800` (30분) - 너무 느림, 변동 놓칠 수 있음

#### 📊 **K200_CHECK_INTERVAL_MIN**
```bash
K200_CHECK_INTERVAL_MIN=30  # 30분 권장
```
- K200 선물만 별도로 체크하는 주기 (분 단위)
- DB증권 API 호출 비용 관리
- **권장값**: `30` (30분) - API 부하 균형

#### 🎯 **VIX_FILTER_THRESHOLD**
```bash
VIX_FILTER_THRESHOLD=0.8  # 0.8% 권장
```
- VIX 알림 필터링 임계값 (%로 입력)
- S&P 500 / NASDAQ 변동이 이 값 미만이면 VIX 알림 무시
- **권장값**: `0.8` - 중요한 변동만 캐치

#### 📍 **DB_FUTURES_CODE**
```bash
DB_FUTURES_CODE=101RB000  # 2025년 12월물 (현재)
```
- K200 선물 종목 코드
- **정기 업데이트 필요** (분기별 롤오버)
- 최근월물 확인: [DB증권 사이트](https://www.dbsec.com)

---

## 권장 설정값

### 🟢 **실전 운영 (Production)**

```bash
# Sentinel (메인 서버)
LOG_LEVEL=INFO
DBSEC_ROUTER_ENABLE=false
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SENTINEL_KEY=your_production_key

# Sentinel-Worker (감시자)
LOG_LEVEL=INFO
FORCE_MARKET_OPEN=true        # 야간 선물 감시 필수!
WATCH_INTERVAL_SEC=180        # 3분 - 실시간 감시
K200_CHECK_INTERVAL_MIN=30    # 30분 - DB증권 API 부하 관리
VIX_FILTER_THRESHOLD=0.8      # 0.8% - 중요한 변동만
DBSEC_ENABLE=true             # K200 선물 감시
DB_FUTURES_CODE=101RB000      # 현재 최근월물
DB_API_BASE=https://openapi.dbsec.co.kr:8443
DB_APP_KEY=...
DB_APP_SECRET=...
SENTINEL_BASE_URL=...
SENTINEL_KEY=...
```

### 🟡 **디버깅 모드**

```bash
# Sentinel-Worker만 변경
LOG_LEVEL=DEBUG               # 상세 로그
FORCE_MARKET_OPEN=true        # 항상 감시
WATCH_INTERVAL_SEC=60         # 1분 - 빠른 테스트
K200_CHECK_INTERVAL_MIN=5     # 5분 - 빠른 K200 체크
```

---

## 문제 해결

### ❌ **야간 선물 알림이 안 와요**

**체크리스트:**
1. ✅ `FORCE_MARKET_OPEN=true` 설정 확인
2. ✅ `DBSEC_ENABLE=true` 설정 확인
3. ✅ `DB_APP_KEY`, `DB_APP_SECRET` 정확한지 확인
4. ✅ `DB_FUTURES_CODE=101RB000` (현재 최근월물) 확인
5. ✅ Sentinel-Worker 로그에서 `"📊 K200 선물 체크 시작..."` 메시지 확인
6. ✅ 시간이 18:00-05:00 KST 범위 내인지 확인

**로그 확인:**
```bash
# Railway에서 Sentinel-Worker 로그 확인
railway logs --service sentinel-worker | grep "K200"
```

### ❌ **알림이 너무 늦게 와요**

**원인:** `WATCH_INTERVAL_SEC=1800` (30분)으로 설정됨

**해결:**
```bash
WATCH_INTERVAL_SEC=180  # 3분으로 변경
```

### ❌ **S&P 500 알림만 오고 다른 건 안 와요**

**원인:** VIX 필터가 너무 높게 설정되었거나, 다른 지수의 변동이 임계값에 미달

**해결:**
```bash
VIX_FILTER_THRESHOLD=0.8  # 0.8%로 설정 (권장)
```

또는 로그 확인:
```bash
railway logs --service sentinel-worker | grep "레벨 판정"
```

### ❌ **중복 알림이 와요**

**원인:** `DBSEC_ROUTER_ENABLE=true`로 설정되어 DB증권 라우터와 Market Watcher가 둘 다 K200 감시

**해결 (Sentinel 메인 서버):**
```bash
DBSEC_ROUTER_ENABLE=false  # 반드시 false로 설정!
```

### ❌ **DB증권 API 호출 실패**

**체크리스트:**
1. ✅ API 키 유효기간 확인 (만료되었을 수 있음)
2. ✅ `DB_API_BASE` URL 정확한지 확인 (기본값: `https://openapi.dbsec.co.kr:8443`)
3. ✅ 방화벽/네트워크 이슈 확인
4. ✅ DB증권 API 서버 상태 확인

**로그에서 에러 확인:**
```bash
railway logs --service sentinel-worker | grep "DB증권"
```

---

## 🎯 빠른 시작 체크리스트

### Sentinel (메인 서버)
- [ ] `TELEGRAM_BOT_TOKEN` 설정
- [ ] `TELEGRAM_CHAT_ID` 설정
- [ ] `SENTINEL_KEY` 설정
- [ ] `DBSEC_ROUTER_ENABLE=false` 설정
- [ ] `LOG_LEVEL=INFO` 설정

### Sentinel-Worker (감시자)
- [ ] `FORCE_MARKET_OPEN=true` 설정 (야간 선물 필수!)
- [ ] `WATCH_INTERVAL_SEC=180` 설정 (3분)
- [ ] `DBSEC_ENABLE=true` 설정
- [ ] `K200_CHECK_INTERVAL_MIN=30` 설정
- [ ] `DB_FUTURES_CODE=101RB000` 설정
- [ ] `DB_APP_KEY` 설정
- [ ] `DB_APP_SECRET` 설정
- [ ] `DB_API_BASE` 설정 (기본값 사용 가능)
- [ ] `VIX_FILTER_THRESHOLD=0.8` 설정
- [ ] `SENTINEL_BASE_URL` 설정
- [ ] `SENTINEL_KEY` 설정

---

## 📝 참고 문서

- [FINAL_DEPLOYMENT.md](./FINAL_DEPLOYMENT.md) - 전체 배포 가이드
- [K200_FUTURES_GUIDE.md](./K200_FUTURES_GUIDE.md) - K200 선물 통합 가이드
- [README.md](./README.md) - 프로젝트 개요

---

## 🆘 추가 지원

문제가 계속되면:
1. Railway 로그 전체 확인: `railway logs --service sentinel-worker`
2. `market_state.json` 파일 확인: 상태가 정상인지 체크
3. DB증권 API 상태 페이지 확인
4. 해당 시간대가 실제 거래 시간인지 확인 (주말/공휴일 제외)

---

**마지막 업데이트:** 2025-10-10  
**버전:** 2.0 (야간 선물 감시 최적화)
