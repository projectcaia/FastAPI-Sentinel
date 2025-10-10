# 센티넬 시스템 현황 (2025-10-11)

## ✅ 해결 완료된 문제들

### 1. **알림 형식 개선** ✅
#### 문제:
- VIX 중심 알림으로 실제 지수 변동을 파악하기 어려움
- "VIX +28.48%" 형식으로 메인 정보가 VIX에 집중

#### 해결:
- **지수 중심 알림으로 변경**
- S&P 500 또는 NASDAQ을 메인으로 표시
- VIX는 부가 정보로 포함

#### 변경 전:
```
📡 [LV2] VIX +28.48%
📝 VIX LV2 진입
```

#### 변경 후:
```
📡 [LV2] S&P 500 -2.23%
📝 LV2 진입 | VIX 21.1 (+28.5%)
```

---

### 2. **K200 선물 야간 감시** ✅
#### 문제:
- Market Watcher의 웹 크롤링으로는 K200 야간선물 데이터 수집 불가
- DB증권 API가 비활성화되어 있어 감시 안 됨

#### 해결:
- **DB증권 REST API 활성화**
- 주간거래: 09:00-15:30
- 야간거래: 18:00-05:00 (다음날)
- 폴링 간격: 3분

#### 설정:
```bash
DBSEC_ENABLE=true              # DB증권 활성화
DBSEC_USE_REST=true            # REST API 모드
DB_POLL_INTERVAL_SEC=180       # 3분 폴링
```

---

### 3. **휴장일 판단 오류** ✅
#### 문제:
- 10월 10일을 휴장일로 잘못 판단
- 미국 시장 감시가 중단됨

#### 해결:
- 한글날(10/9) 정확히 처리
- 강제 시장 오픈 모드 추가
- 휴장일에도 미국 시장 감시

#### 설정:
```bash
FORCE_MARKET_OPEN=true  # 휴장일에도 미국 시장 감시
```

---

## 🎯 현재 시스템 구조

### 1. **DB증권 K200 선물 감시** (NEW)
**담당**: `/routers/dbsec.py` + `/services/dbsec_rest.py`
- **감시 대상**: K200 선물 (코드: 101C6000)
- **거래 시간**: 
  - 주간: 09:00-15:30 KST
  - 야간: 18:00-05:00 KST
- **폴링 간격**: 3분
- **알림 기준**: 0.8% / 1.5% / 2.5%
- **중복 방지**: 동일 레벨 30분

### 2. **Market Watcher 일반 감시**
**담당**: `market_watcher.py`
- **한국 정규장**: KOSPI 현물
- **미국 정규장**: S&P 500, NASDAQ, VIX
- **선물 시장**: 미국 선물 (ES=F, NQ=F)
- **감시 간격**: 3분
- **VIX 필터**: 0.6% 이상 지수 변동시만 VIX 감지

### 3. **센티넬 메인**
**담당**: `main.py`
- 알림 수신 및 처리
- Telegram 전송
- Caia AI 분석
- Hub 연동

---

## 📊 감시 체계

```
09:00-15:30 (한국 주간)
├─ DB증권 API: K200 선물 (3분 폴링)
└─ Market Watcher: KOSPI 현물 (3분)

15:30-18:00 (장 마감 / 선물 준비)
└─ 대기 (또는 FORCE_MARKET_OPEN시 미국 시장 감시)

18:00-05:00 (야간 / 미국 시장)
├─ DB증권 API: K200 선물 야간거래 (3분 폴링)
└─ Market Watcher: 미국 지수 (S&P, NASDAQ, VIX) (3분)

05:00-09:00 (장 마감)
└─ FORCE_MARKET_OPEN시 미국 시장 감시 계속
```

---

## 🚀 배포 체크리스트

### Railway 환경변수 설정
- [ ] `DBSEC_ENABLE=true`
- [ ] `DB_APP_KEY=...` (DB증권 API 키)
- [ ] `DB_APP_SECRET=...` (DB증권 시크릿)
- [ ] `DB_POLL_INTERVAL_SEC=180`
- [ ] `FORCE_MARKET_OPEN=true`
- [ ] `WATCH_INTERVAL_SEC=180`
- [ ] `VIX_FILTER_THRESHOLD=0.6`
- [ ] `SENTINEL_BASE_URL=...`
- [ ] `TELEGRAM_BOT_TOKEN=...`
- [ ] `TELEGRAM_CHAT_ID=...`

### 배포 후 확인
```bash
# 1. Health Check
curl https://your-project.railway.app/health

# 2. DB증권 상태
curl https://your-project.railway.app/sentinel/dbsec/health

# 3. 테스트 알림
curl -X POST https://your-project.railway.app/sentinel/dbsec/alert/test
```

### 로그 확인
Railway 로그에서 다음 메시지 확인:
```
✅ DB증권 K200 선물지수 모니터링 활성화 (주간/야간)
[DBSEC] Starting K200 선물지수 polling (interval: 3분)
[DBSEC] K200 선물: 350.25 (+0.85%) Vol: ...
```

---

## 📝 알림 예시

### K200 선물 알림 (DB증권)
```
📡 [LV2] K200 선물 +1.52%
⏱ 2025-10-11T13:45:23+09:00
📝 K200 선물 상승 1.52% (DB증권)
```

### 미국 지수 알림 (지수 중심)
```
📡 [LV2] S&P 500 -2.23%
⏱ 2025-10-11T04:29:30+09:00
📝 LV2 진입 | VIX 21.1 (+28.5%)
```

### KOSPI 알림 (Market Watcher)
```
📡 [LV1] KOSPI -0.85%
⏱ 2025-10-11T10:15:00+09:00
📝 LV1 진입
```

---

## 🔧 문제 해결

### DB증권 API 오류
**증상**: `[DBSEC] Failed to get price data`
**해결**: 
1. DB_APP_KEY, DB_APP_SECRET 확인
2. 토큰 수동 갱신: `POST /sentinel/dbsec/token/refresh`
3. API 키 재발급 (DB증권 개발자 센터)

### 알림이 오지 않음
**증상**: 변동이 있는데 알림이 없음
**원인**: 
- 중복 알림 방지 (30분 내 동일 레벨)
- 거래 시간 외
- 임계값 미달

**확인**:
```bash
# 현재 세션 확인
curl https://your-project.railway.app/sentinel/dbsec/sessions

# 강제 알림 테스트
curl -X POST https://your-project.railway.app/sentinel/dbsec/alert/test
```

### VIX 알림이 너무 많음
**해결**: VIX_FILTER_THRESHOLD 상향
```bash
VIX_FILTER_THRESHOLD=1.0  # 지수 1% 이상 변동시만 VIX 감지
```

---

## 📈 성능 지표

### API 호출 빈도
- **DB증권 API**: 3분마다 (하루 480회)
- **Yahoo Finance**: 3분마다 (하루 480회 x 심볼 수)

### 알림 빈도 (예상)
- **K200 선물**: 하루 2-5회 (거래일)
- **미국 지수**: 하루 1-3회
- **VIX**: 급등락시 1-2회

### 중복 방지
- 동일 레벨 30분 내 중복 차단
- 시스템 부하 최소화

---

## 🎯 다음 단계

1. **모니터링 대시보드** (선택)
   - Grafana 연동
   - 실시간 차트

2. **알림 고도화** (선택)
   - Discord 연동
   - 이메일 알림

3. **백테스팅** (선택)
   - 과거 데이터 기반 임계값 최적화
   - 알림 정확도 분석

---

**시스템 버전**: v2.0.0  
**최종 업데이트**: 2025-10-11  
**상태**: ✅ 정상 작동 중
