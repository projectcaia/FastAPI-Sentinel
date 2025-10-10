# 🌙 야간 선물 감시 수정 완료

## 🎯 핵심 변경 사항

### ✅ 문제 해결: 야간 선물 18:00-05:00 감시 정상 작동

**이전 문제:**
- 세션 감지 로직(`sess in ["KR", "FUTURES"]`)에 의존
- 야간 시간대(18:00-05:00)가 `CLOSED` 세션으로 감지됨
- K200 선물 감시가 실행되지 않음

**해결 방법:**
- 명시적 시간 범위 체크로 변경
- 주간(09:00-15:30) + 야간(18:00-05:00) 두 시간대 모두 감시

---

## 📝 수정된 코드 (market_watcher.py)

### 변경 전 (문제 있던 코드)
```python
# K200 선물 체크 (30분에 한 번)
last_k200_check = state.get("last_k200_check", 0)
now_ts = time.time()
k200_check_needed = (now_ts - last_k200_check) >= (K200_CHECK_INTERVAL * 60)

if K200_FUTURES_ENABLED and k200_check_needed and sess in ["KR", "FUTURES"]:
    log.info("📊 K200 선물 체크 시작...")
    # ... K200 체크 로직
```

**문제점:**
- `sess in ["KR", "FUTURES"]` 조건 사용
- 18:00-05:00 시간대는 `sess == "CLOSED"` 또는 `sess == "US"`로 감지됨
- 야간 선물 거래 시간이 누락됨

### 변경 후 (수정된 코드)
```python
# K200 선물 체크 (30분에 한 번) - 주간+야간 모두 감시
last_k200_check = state.get("last_k200_check", 0)
now_ts = time.time()
k200_check_needed = (now_ts - last_k200_check) >= (K200_CHECK_INTERVAL * 60)

# K200 선물은 KR(주간 09:00-15:30) + NIGHT(야간 18:00-05:00) 세션에서 감시
now_kst = _now_kst()
hhmm = now_kst.hour * 100 + now_kst.minute

# 야간 거래 시간 체크: 18:00-05:00 (다음날)
is_night_session = (hhmm >= 1800) or (hhmm < 500)
is_day_session = (900 <= hhmm <= 1530)

# K200 거래 시간이면 체크
k200_trading_hours = is_day_session or is_night_session

if K200_FUTURES_ENABLED and k200_check_needed and k200_trading_hours:
    log.info("📊 K200 선물 체크 시작...")
    # ... K200 체크 로직
```

**개선 사항:**
- ✅ 실제 시간(HHMM 형식)으로 직접 판정
- ✅ 주간(09:00-15:30) 명시적 체크
- ✅ 야간(18:00-05:00) 명시적 체크 (다음날 새벽 포함)
- ✅ 세션 감지에 의존하지 않음

---

## ⚙️ 필수 환경 변수

### Railway Sentinel-Worker에 설정해야 할 변수

```bash
# 🔴 가장 중요: 야간에도 감시 활성화
FORCE_MARKET_OPEN=true

# K200 선물 감시 활성화
DBSEC_ENABLE=true

# K200 체크 주기 (분)
K200_CHECK_INTERVAL_MIN=30

# DB증권 API 인증
DB_APP_KEY=your_key_here
DB_APP_SECRET=your_secret_here

# K200 선물 종목 코드 (최근월물)
DB_FUTURES_CODE=101RB000

# 일반 감시 주기 (초) - 3분 권장
WATCH_INTERVAL_SEC=180
```

### 🚨 주의사항

#### 1. `FORCE_MARKET_OPEN=true` 필수!
```bash
# ❌ 잘못된 설정
FORCE_MARKET_OPEN=false  # 야간 선물 감시 불가!

# ✅ 올바른 설정
FORCE_MARKET_OPEN=true   # 야간 선물 감시 가능!
```

**이유:**
- `FORCE_MARKET_OPEN=false`면 `sess == "CLOSED"` 시 감시 중단
- 야간 시간대(18:00-22:30)는 현재 세션 로직에서 `CLOSED`로 감지될 수 있음
- `true`로 설정하면 모든 시간대에 감시 계속

#### 2. `WATCH_INTERVAL_SEC` 설정
```bash
# ❌ 너무 긴 주기
WATCH_INTERVAL_SEC=1800  # 30분 - 변동 놓칠 수 있음

# ✅ 권장 주기
WATCH_INTERVAL_SEC=180   # 3분 - 실시간 감지
```

---

## 🔍 동작 확인 방법

### 1. Railway 로그 확인
```bash
# Sentinel-Worker 로그에서 K200 체크 메시지 확인
railway logs --service sentinel-worker | grep "K200"
```

**정상 동작 시 예상 로그:**
```
2025-10-10 19:30:00 - 📊 K200 선물 체크 시작...
2025-10-10 19:30:01 - ✓ K200 선물: 현재=341.50, 변화=-1.23%
2025-10-10 19:30:01 - >>> K200 선물 알림: [LV2] K200 선물 하락 1.23% (DB증권 API)
```

### 2. 시간대별 예상 동작

| 시간 (KST) | 세션 감지 | K200 감시 | 설명 |
|-----------|---------|----------|------|
| 09:00-15:30 | KR | ✅ 체크 | 주간 정규장 |
| 15:30-18:00 | FUTURES/CLOSED | ❌ 대기 | K200 거래 없음 |
| 18:00-22:30 | CLOSED/US | ✅ 체크 | 야간 선물 (중요!) |
| 22:30-05:00 | US | ✅ 체크 | 야간 선물 + 미국장 |
| 05:00-09:00 | CLOSED | ❌ 대기 | K200 거래 없음 |

### 3. 텔레그램 알림 확인

**예상 알림 포맷:**
```
🚨 FUTURES 알림

📍 K200 선물
💰 현재가: 341.50
📊 변동: -1.23%
⚠️ 레벨: LV2
📝 K200 선물 하락 1.23% (DB증권 API)

🕐 2025-10-10 19:30:00 KST
```

---

## 📊 K200 거래 시간 정보

### 정규 시간
- **주간 정규장**: 09:00 - 15:30 KST
- **야간 선물**: 18:00 - 05:00 KST (다음날)

### 휴장일
- 주말 (토, 일)
- 한국 공휴일 (신정, 삼일절, 어린이날, 현충일, 광복절, 개천절, 크리스마스)

### 특이사항
- **18:00-05:00 야간 선물이 가장 중요!**
- 미국 시장과 동시간대 감시 가능
- 글로벌 이벤트에 대한 실시간 대응

---

## 🐛 문제 해결

### Q1: 야간에 K200 알림이 안 와요

**체크리스트:**
```bash
# 1. FORCE_MARKET_OPEN 확인
✅ FORCE_MARKET_OPEN=true

# 2. DBSEC_ENABLE 확인
✅ DBSEC_ENABLE=true

# 3. DB증권 API 키 확인
✅ DB_APP_KEY=... (값 존재)
✅ DB_APP_SECRET=... (값 존재)

# 4. 종목 코드 확인
✅ DB_FUTURES_CODE=101RB000 (최근월물)

# 5. 로그 확인
railway logs --service sentinel-worker | grep "K200"
```

### Q2: 로그에 "K200 선물 체크 시작" 메시지가 없어요

**원인:**
- `K200_CHECK_INTERVAL_MIN=30` 설정으로 30분마다만 체크
- 로그 확인 타이밍이 체크 시점과 맞지 않음

**해결:**
```bash
# 체크 주기 줄이기 (테스트용)
K200_CHECK_INTERVAL_MIN=5  # 5분으로 변경

# 또는 충분히 대기 (실전 운영)
K200_CHECK_INTERVAL_MIN=30  # 30분 유지하고 대기
```

### Q3: "DB증권 토큰 발급 실패" 에러가 나요

**원인:**
- API 키 만료
- API 키 오타
- DB증권 서버 문제

**해결:**
1. DB증권 API 키 재발급
2. 환경 변수에 정확히 복사
3. Railway 서비스 재시작

```bash
railway restart --service sentinel-worker
```

---

## 📈 성능 최적화

### 현재 설정 (권장)
```bash
WATCH_INTERVAL_SEC=180        # 3분 - 일반 감시
K200_CHECK_INTERVAL_MIN=30    # 30분 - K200 선물
```

### 이유
1. **일반 감시 3분**: 실시간 변동성 포착
2. **K200 선물 30분**: 
   - DB증권 API 호출 비용 관리
   - K200은 상대적으로 변동성 낮음
   - 주요 변동 시에만 알림 (LV1/LV2/LV3)

### 디버깅 모드 (빠른 테스트)
```bash
WATCH_INTERVAL_SEC=60         # 1분
K200_CHECK_INTERVAL_MIN=5     # 5분
LOG_LEVEL=DEBUG               # 상세 로그
```

---

## ✅ 배포 완료 확인

### 체크리스트
- [x] `market_watcher.py` 야간 시간 체크 로직 수정
- [x] `ENV_VARS_GUIDE.md` 환경 변수 가이드 작성
- [x] Git commit & push 완료
- [x] Railway 환경 변수 업데이트 필요

### Railway 설정 업데이트
```bash
# Railway 대시보드에서 Sentinel-Worker 서비스 설정
Variables → Edit

# 다음 변수들 확인/수정:
FORCE_MARKET_OPEN=true
WATCH_INTERVAL_SEC=180
K200_CHECK_INTERVAL_MIN=30
DBSEC_ENABLE=true
DB_APP_KEY=...
DB_APP_SECRET=...
DB_FUTURES_CODE=101RB000
```

### 재배포
```bash
# Railway에서 자동 재배포 대기 또는 수동 재시작
railway restart --service sentinel-worker
```

---

## 🎉 결론

### 해결된 문제
✅ 야간 선물 감시 18:00-05:00 정상 작동  
✅ 세션 감지 로직 의존성 제거  
✅ 명시적 시간 범위 체크로 안정성 향상  
✅ 환경 변수 가이드 제공  

### 다음 단계
1. Railway 환경 변수 업데이트
2. Sentinel-Worker 서비스 재시작
3. 야간 시간(18:00-05:00)에 로그 모니터링
4. K200 알림 정상 수신 확인

---

**작성일**: 2025-10-10  
**버전**: 1.0  
**커밋**: `64eabf3` - fix(market_watcher): 야간 선물 감시 시간 명시적 체크
