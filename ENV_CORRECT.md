# ✅ 올바른 환경 변수 설정 (최종)

## 🚨 중요: Railway 설정을 이렇게 바꾸세요!

---

## Sentinel-Worker 환경 변수 (필수!)

### ❌ 잘못된 설정 (현재)

```bash
FORCE_MARKET_OPEN=true          # ❌ 주말에도 감시하게 됨
WATCH_INTERVAL_SEC=180          # ❌ 3분마다 감시 (너무 자주)
```

### ✅ 올바른 설정 (수정해야 함)

```bash
# 기본 설정
LOG_LEVEL=INFO
WATCH_INTERVAL_SEC=1800         # ✅ 30분마다 감시

# 강제 모드 비활성화 (휴장일에는 쉬어야 함)
FORCE_MARKET_OPEN=false         # ✅ 주말/공휴일에는 감시 안 함

# VIX 필터
VIX_FILTER_THRESHOLD=0.8

# K200 선물 (DB증권 API)
DBSEC_ENABLE=true
K200_CHECK_INTERVAL_MIN=30
DB_FUTURES_CODE=101RB000
DB_API_BASE=https://openapi.dbsec.co.kr:8443
DB_APP_KEY=your_db_app_key
DB_APP_SECRET=your_db_app_secret

# Sentinel 연결
SENTINEL_BASE_URL=https://your-sentinel-url.railway.app
SENTINEL_KEY=your_sentinel_key
```

---

## 📊 정상 작동 시간표

| 시간 (KST) | 요일 | 세션 | 감시 대상 |
|-----------|------|------|----------|
| **09:00-15:30** | **평일** | **KR** | **KOSPI, KODEX, TIGER, KS200 + K200 선물** |
| 15:30-18:00 | 평일 | CLOSED | 대기 |
| **18:00-22:30** | **평일** | **FUTURES** | **미국 선물 + K200 선물 (야간)** |
| **22:30-05:00** | **평일** | **US** | **S&P 500, NASDAQ, VIX + K200 선물 (야간)** |
| 05:00-09:00 | 평일 | CLOSED | 대기 |
| **전체** | **주말** | **CLOSED** | **감시 안 함** |
| **전체** | **공휴일** | **CLOSED** | **감시 안 함** |

---

## 🔄 시스템 로직 (수정됨)

### 1. 휴장일 체크 (최우선)
```python
# 주말인가?
if now_kst.weekday() >= 5:  # 토, 일
    return  # 모든 감시 중단

# 공휴일인가?
if (month, day) in kr_holidays:
    return  # 모든 감시 중단
```

### 2. 세션 감지
```python
hhmm = hour * 100 + minute

if 900 <= hhmm <= 1530:      # 09:00-15:30
    session = "KR"
elif 2230 <= hhmm or hhmm < 500:  # 22:30-05:00
    session = "US"
elif 1530 < hhmm < 2230:     # 15:30-22:30
    session = "FUTURES"
else:
    session = "CLOSED"
```

### 3. K200 선물 체크 (30분마다)
```python
# 거래 시간인가?
is_day = (900 <= hhmm <= 1530)      # 주간 09:00-15:30
is_night = (hhmm >= 1800) or (hhmm < 500)  # 야간 18:00-05:00

# 평일이고 거래 시간이면 체크
if not is_weekend and not is_holiday and (is_day or is_night):
    check_k200_futures()
```

### 4. 일반 지수 감시 (30분마다)
```python
if session == "KR":
    watch_symbols = ["KOSPI", "KODEX", "TIGER", "KS200"]
elif session == "US":
    watch_symbols = ["S&P 500", "NASDAQ", "VIX"]
elif session == "FUTURES":
    watch_symbols = ["ES", "NQ"]  # 미국 선물
```

---

## 🐛 현재 문제 분석

### 문제 1: 토요일 오후에 미국장 감시
**원인:** `FORCE_MARKET_OPEN=true` 설정

**로그:**
```
2025-10-11 05:46:26,975 - 시장 체크 시작 [세션: CLOSED] 2025-10-11 14:46:26 KST
2025-10-11 05:46:26,975 - 🔴 강제 시장 오픈 모드 활성화 - 휴장일에도 감시 계속
2025-10-11 05:46:27,742 - 🔴 강제 모드: 휴장 중에도 미국 시장 감시
2025-10-11 05:46:27,742 - 【미국 정규장】 데이터 수집 중...
```

**해결:**
```bash
FORCE_MARKET_OPEN=false  # ← 이렇게 변경!
```

**수정 후 예상 로그:**
```
2025-10-11 14:46:26 - 시장 체크 시작 [세션: CLOSED] 2025-10-11 14:46:26 KST
2025-10-11 14:46:26 - 주말 휴장 - 모든 시장 감시 중단
```

### 문제 2: 3분마다 감시 (너무 자주)
**원인:** `WATCH_INTERVAL_SEC=180` (3분)

**로그 시간 간격:**
```
05:30:18 → 05:33:30 (3분 12초)
05:33:30 → 05:36:47 (3분 17초)
05:36:47 → 05:40:02 (3분 15초)
```

**해결:**
```bash
WATCH_INTERVAL_SEC=1800  # 30분 (사용자 요청)
```

**수정 후 예상 간격:**
```
05:00:00 → 05:30:00 (30분)
05:30:00 → 06:00:00 (30분)
```

### 문제 3: 코스피 지수들 감시 안 됨
**원인:** 기존 코드에서 `KR_SPOT_PRIORITY[:1]` (KOSPI만)

**기존 코드:**
```python
if sess == "KR":
    symbols = KR_SPOT_PRIORITY[:1]  # KOSPI만
```

**수정된 코드:**
```python
if sess == "KR":
    symbols = KR_SPOT_PRIORITY  # 전체: KOSPI, KODEX, TIGER, KS200
```

**수정 후 예상 로그 (평일 09:00-15:30):**
```
【한국 정규장】 데이터 수집 중...
✓ KOSPI: 현재=2543.21, 전일대비=-0.52%, 일중변동=0.35%
✓ KODEX 200: 현재=33450, 전일대비=-0.48%, 일중변동=0.28%
✓ TIGER 200: 현재=33420, 전일대비=-0.49%, 일중변동=0.29%
✓ KOSPI 200: 현재=341.50, 전일대비=-0.50%, 일중변동=0.30%
📊 K200 선물 체크 시작...
✓ K200 선물: 현재=341.75, 변화=-0.45%
```

### 문제 4: DB증권 API 403 에러
**원인:** API 키 문제 또는 주말/공휴일 API 접근 제한

**로그:**
```
2025-10-11 05:43:16,366 - ERROR - DB증권 토큰 발급 실패: 403
```

**해결:**
1. DB증권 API 키 재확인
2. 주말에는 API 자체가 막혀있을 수 있음 (정상)
3. 평일 거래 시간에 다시 테스트

---

## 🎯 Railway 설정 변경 단계

### 1. Railway 대시보드 접속
```
https://railway.app
→ sentinel-worker 서비스 선택
→ Variables 탭
```

### 2. 다음 변수들 수정
```bash
❌ FORCE_MARKET_OPEN=true
✅ FORCE_MARKET_OPEN=false

❌ WATCH_INTERVAL_SEC=180
✅ WATCH_INTERVAL_SEC=1800
```

### 3. 서비스 재시작
```
Railway에서 자동 재배포 대기 (약 1-2분)
또는
railway restart --service sentinel-worker
```

### 4. 로그 확인
```bash
railway logs --service sentinel-worker
```

**주말/공휴일 예상 로그:**
```
시장 체크 시작 [세션: CLOSED] 2025-10-11 14:46:26 KST
주말 휴장 - 모든 시장 감시 중단
```

**평일 정규장 예상 로그 (09:00-15:30):**
```
시장 체크 시작 [세션: KR] 2025-10-15 10:30:00 KST
📊 K200 선물 감시 활성화 (DB증권 API)
📊 K200 선물 체크 시작...
✓ K200 선물: 현재=341.50, 변화=-0.50%
【한국 정규장】 데이터 수집 중...
✓ KOSPI: 현재=2543.21, 전일대비=-0.52%
✓ KODEX 200: 현재=33450, 전일대비=-0.48%
✓ TIGER 200: 현재=33420, 전일대비=-0.49%
✓ KOSPI 200: 현재=341.50, 전일대비=-0.50%
체크 완료
```

**평일 야간 예상 로그 (18:00-05:00):**
```
시장 체크 시작 [세션: US] 2025-10-15 23:00:00 KST
📊 K200 선물 감시 활성화 (DB증권 API)
📊 K200 선물 체크 시작...
✓ K200 선물: 현재=341.75, 변화=-0.45% (야간)
【미국 정규장】 데이터 수집 중...
✓ S&P 500: 현재=6552.51, 전일대비=-2.71%
✓ NASDAQ: 현재=22204.43, 전일대비=-3.56%
✓ VIX: 값=21.66, 변화율=31.83%
체크 완료
```

---

## ✅ 수정 완료 체크리스트

### Railway 설정
- [ ] `FORCE_MARKET_OPEN=false` 설정
- [ ] `WATCH_INTERVAL_SEC=1800` 설정
- [ ] 서비스 재시작
- [ ] 로그 확인

### 코드 수정 (완료)
- [x] 주말 휴장 체크
- [x] 공휴일 휴장 체크
- [x] K200 선물 시간대 체크 (주간+야간, 평일만)
- [x] 한국 지수 전체 감시 (KOSPI, KODEX, TIGER, KS200)
- [x] FORCE_MARKET_OPEN 로직 수정

### 테스트 (평일에 확인)
- [ ] 평일 09:00-15:30: 한국 지수 + K200 선물 감시
- [ ] 평일 18:00-05:00: 미국 지수 + K200 선물 야간 감시
- [ ] 주말: 모든 감시 중단
- [ ] 30분 간격으로 체크

---

## 📞 문제 해결

### Q: 주말에도 계속 로그가 나와요
**A:** `FORCE_MARKET_OPEN=false`로 변경하고 재시작하세요.

### Q: 3분마다 체크되고 있어요
**A:** `WATCH_INTERVAL_SEC=1800`으로 변경하고 재시작하세요.

### Q: 코스피 알림이 안 와요
**A:** 코드 수정 완료, GitHub에서 pull하고 Railway 재배포하세요.

### Q: K200 선물이 403 에러
**A:** 
1. 주말/공휴일에는 DB증권 API가 막혀있을 수 있음 (정상)
2. API 키 재확인
3. 평일 거래 시간(09:00-15:30, 18:00-05:00)에 다시 테스트

---

**작성일:** 2025-10-11  
**버전:** 2.0 (전면 수정)  
**커밋:** `d8b86d6` - fix(critical): 시장 감시 로직 전면 수정

**가장 중요한 것:**
```bash
FORCE_MARKET_OPEN=false      # 주말에 쉬어야 함!
WATCH_INTERVAL_SEC=1800      # 30분마다 체크!
```
