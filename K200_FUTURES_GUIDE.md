# K200 선물 감시 가이드

## 🎯 시스템 구조 변경

### ❌ 기존 방식 (문제)
```
센티넬 (main.py)
└─ DB증권 라우터 (routers/dbsec.py)
   └─ REST API 폴러 (3분 폴링)
      └─ 알림 전송 (작동 안 함)

Market Watcher (별도)
└─ Yahoo Finance (웹 크롤링)
   └─ 야간 선물 불가 ❌
```

### ✅ 새로운 방식 (해결)
```
Market Watcher (market_watcher.py)
├─ Yahoo Finance: KOSPI, S&P, NASDAQ, VIX
└─ DB증권 API: K200 선물 (주간+야간)
   ├─ 30분마다 체크
   ├─ 직접 토큰 발급
   └─ 알림 전송 ✅
```

---

## 🔧 Railway 환경변수 설정

### 필수 설정
```bash
# DB증권 API 인증
DB_APP_KEY=your_db_app_key
DB_APP_SECRET=your_db_app_secret

# K200 선물 감시 활성화
DBSEC_ENABLE=true

# 선물 코드 (현재 월물)
DB_FUTURES_CODE=101C6000

# K200 체크 간격 (분)
K200_CHECK_INTERVAL_MIN=30

# DB증권 라우터 비활성화 (중요!)
DBSEC_ROUTER_ENABLE=false
```

### 기타 설정
```bash
# Market Watcher 일반 감시 간격
WATCH_INTERVAL_SEC=180

# 센티넬 엔드포인트
SENTINEL_BASE_URL=https://your-project.railway.app

# 강제 시장 오픈
FORCE_MARKET_OPEN=true
```

---

## 📊 K200 선물 감시 로직

### 체크 타이밍
- **간격**: 30분마다 (기존 시스템과 동일)
- **세션**: KR (주간) 또는 FUTURES (야간)
- **시간**:
  - 주간: 09:00-15:30
  - 야간: Market Watcher는 야간 체크 안 함 (FUTURES 세션은 미국 선물)

### 알림 기준
```python
LV1: ±0.8% 이상
LV2: ±1.5% 이상
LV3: ±2.5% 이상
```

### 중복 방지
- 동일 레벨 변경시에만 알림
- `prev_level != current_level`

---

## 🔍 작동 확인

### 1. Market Watcher 로그 확인
```
✅ 정상:
📊 K200 선물 감시 활성화 (DB증권 API)
📊 K200 선물 체크 시작...
✓ K200 선물: 현재=350.25, 변화=+1.52%
>>> K200 선물 알림: [LV2] K200 선물 상승 1.52% (DB증권 API)

❌ 오류:
⚠ K200 선물 데이터 수집 실패
DB증권 토큰 발급 실패: 401
```

### 2. 텔레그램 알림 예시
```
📡 [LV2] K200 선물 +1.52%
⏱ 2025-10-11T13:45:23+09:00
📝 K200 선물 상승 1.52% (DB증권 API)
```

---

## 🚀 배포 순서

### 1. Git Pull
```bash
git pull origin main
```

### 2. Railway 환경변수 설정
```
DBSEC_ENABLE=true
DB_APP_KEY=...
DB_APP_SECRET=...
K200_CHECK_INTERVAL_MIN=30
DBSEC_ROUTER_ENABLE=false  # 중요!
```

### 3. Railway Redeploy
- Railway 대시보드에서 수동 배포
- 또는 자동 배포 대기

### 4. 로그 확인
```
✅ K200 선물 감시: Market Watcher에서 DB증권 API 직접 호출
📊 K200 선물 감시 활성화 (DB증권 API)
```

---

## 🐛 문제 해결

### Q: K200 선물 알림이 안 와요
**A**: 다음을 확인하세요
1. `DBSEC_ENABLE=true` 설정 확인
2. `DB_APP_KEY`, `DB_APP_SECRET` 확인
3. 변동률이 0.8% 이상인지 확인
4. 30분 체크 주기 확인 (바로 안 올 수 있음)
5. Railway 로그에서 "K200 선물 체크" 메시지 확인

### Q: "DB증권 토큰 발급 실패" 오류
**A**: 
1. API 키 재확인
2. DB증권 개발자 센터에서 키 재발급
3. Railway 환경변수 재설정

### Q: 중복 알림이 와요
**A**: 
- `DBSEC_ROUTER_ENABLE=false` 확인
- DB증권 라우터가 비활성화되어야 함

### Q: 야간 선물 감시가 안 돼요
**A**: 
- Market Watcher는 `KR` 세션(09:00-15:30)에서만 K200 체크
- 야간(18:00-05:00)은 `FUTURES` 세션이지만 미국 선물 감시
- **해결책**: K200 체크를 `FUTURES` 세션에도 추가 필요

---

## 💡 개선 제안

### 야간 선물 감시 추가
`market_watcher.py`의 K200 체크 조건 수정:
```python
# 현재:
if K200_FUTURES_ENABLED and k200_check_needed and sess in ["KR", "FUTURES"]:

# 야간 포함:
if K200_FUTURES_ENABLED and k200_check_needed and sess in ["KR", "FUTURES"]:
```

이미 포함되어 있으므로 야간도 감시됩니다!

---

## 📝 요약

### ✅ 해결된 사항
1. K200 선물 감시를 Market Watcher로 통합
2. DB증권 API 직접 호출 (간단한 토큰 발급)
3. 30분 간격 체크 (기존 시스템과 동일)
4. 중복 감시 제거 (DB증권 라우터 비활성화)

### 🎯 알림 형식
```
지수명: K200 선물
레벨: LV1/LV2/LV3
변동률: 실제 변동률 (%)
노트: K200 선물 상승/하락 X.XX% (DB증권 API)
```

### 📊 감시 일정
- **주간**: 09:00-15:30 (30분마다)
- **야간**: 18:00-05:00 (FUTURES 세션, 30분마다)

---

**버전**: v2.1.0 (K200 통합)  
**업데이트**: 2025-10-11
