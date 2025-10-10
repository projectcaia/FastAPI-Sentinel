# 🚀 센티넬 시스템 최종 배포 가이드

## ✅ 완료된 모든 수정사항

### 1. **알림 형식 개선** ✅
- **VIX 중심** → **지수 중심** 변경
- S&P 500 또는 NASDAQ이 메인, VIX는 부가 정보
- 텔레그램 알림: `[LV2] S&P 500 -2.23% | VIX 21.1 (+28.5%)`

### 2. **K200 선물 감시 통합** ✅
- Market Watcher에서 DB증권 API 직접 호출
- DB증권 라우터 완전 비활성화 (중복 제거)
- 30분 간격 체크 (기존 시스템과 동일)
- 주간(09:00-15:30) + 야간(18:00-05:00) 감시

### 3. **시스템 단순화** ✅
- 센티넬 + 워커 분리 제거
- Market Watcher 단일 시스템으로 통합
- 복잡도 대폭 감소

---

## 🔧 Railway 환경변수 설정 (최종)

### 복사해서 Railway Variables에 붙여넣기:

```bash
# ========== 필수 설정 ==========
# OpenAI & Assistant
OPENAI_API_KEY=your_openai_api_key
CAIA_ASSISTANT_ID=asst_your_id
CAIA_THREAD_ID=thread_your_id

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_token
TELEGRAM_CHAT_ID=your_chat_id

# DB증권 K200 선물
DB_APP_KEY=your_db_app_key
DB_APP_SECRET=your_db_app_secret

# ========== 시스템 설정 ==========
# Sentinel
SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app
LOG_LEVEL=INFO

# K200 선물 감시 (Market Watcher 통합)
DBSEC_ENABLE=true
K200_CHECK_INTERVAL_MIN=30
DB_FUTURES_CODE=101C6000

# DB증권 라우터 비활성화 (중요!)
DBSEC_ROUTER_ENABLE=false

# Market Watcher 설정
WATCH_INTERVAL_SEC=180
VIX_FILTER_THRESHOLD=0.6
FORCE_MARKET_OPEN=true

# ========== 선택 설정 ==========
# Caia 상세 로그 (디버깅용)
CAIA_VERBOSE=0

# 중복 알림 방지
DEDUP_WINDOW_MIN=30
```

---

## 📊 시스템 구조 (최종)

```
┌─────────────────────────────────────────┐
│     Market Watcher (통합 시스템)          │
├─────────────────────────────────────────┤
│                                         │
│  ┌───────────────────────────────┐     │
│  │ Yahoo Finance (웹 크롤링)      │     │
│  │  - KOSPI 현물                  │     │
│  │  - S&P 500, NASDAQ, VIX        │     │
│  │  - 미국 선물 (ES=F, NQ=F)      │     │
│  │  간격: 3분                     │     │
│  └───────────────────────────────┘     │
│                                         │
│  ┌───────────────────────────────┐     │
│  │ DB증권 API (직접 호출)         │     │
│  │  - K200 선물 (주간+야간)       │     │
│  │  - 토큰 자동 발급              │     │
│  │  간격: 30분                    │     │
│  └───────────────────────────────┘     │
│                                         │
│           ↓ 알림 전송                   │
│                                         │
│  ┌───────────────────────────────┐     │
│  │ Sentinel FastAPI               │     │
│  │  /sentinel/alert               │     │
│  └───────────────────────────────┘     │
│                                         │
│           ↓                             │
│                                         │
│  ┌───────────────────────────────┐     │
│  │ Telegram + Caia AI             │     │
│  └───────────────────────────────┘     │
│                                         │
└─────────────────────────────────────────┘
```

---

## 🎯 감시 일정표

| 시간 (KST) | Market Watcher | 감시 대상 |
|-----------|---------------|---------|
| 09:00-15:30 | ✅ 3분 간격 | KOSPI 현물 (Yahoo) |
| 09:00-15:30 | ✅ 30분 간격 | K200 선물 (DB증권) |
| 15:30-18:00 | ❌ 휴식 | - |
| 18:00-22:30 | ✅ 30분 간격 | K200 선물 (DB증권) |
| 22:30-05:00 | ✅ 3분 간격 | S&P 500, NASDAQ, VIX (Yahoo) |
| 05:00-09:00 | ⚠️ 강제 모드시 | 미국 시장 계속 감시 |

---

## 📱 알림 예시

### K200 선물 (DB증권 API)
```
📡 [LV2] K200 선물 +1.52%
⏱ 2025-10-11T13:45:23+09:00
📝 K200 선물 상승 1.52% (DB증권 API)
```

### 미국 지수 (지수 중심)
```
📡 [LV2] S&P 500 -2.23%
⏱ 2025-10-11T04:29:30+09:00
📝 LV2 진입 | VIX 21.1 (+28.5%)
```

### KOSPI (Yahoo Finance)
```
📡 [LV1] KOSPI -0.85%
⏱ 2025-10-11T10:15:00+09:00
📝 LV1 진입
```

---

## 🚀 배포 순서

### 1. Git Pull
```bash
cd /your/project
git pull origin main
```

### 2. Railway 환경변수 설정
1. Railway 대시보드 접속
2. 프로젝트 > Variables 탭
3. 위의 환경변수 복사해서 입력
4. **중요**: `DBSEC_ROUTER_ENABLE=false` 확인

### 3. Railway 배포
- 자동 배포 대기 (GitHub 연동)
- 또는 수동 Deploy 버튼 클릭

### 4. 배포 확인
```bash
# Health Check
curl https://your-project.railway.app/health

# 응답 확인
{
  "status": "ok",
  "version": "sentinel-fastapi-v2-1.4.1-patched",
  ...
}
```

### 5. 로그 모니터링
Railway 로그에서 다음 메시지 확인:
```
✅ K200 선물 감시: Market Watcher에서 DB증권 API 직접 호출
📊 K200 선물 감시 활성화 (DB증권 API)
✓ K200 선물: 현재=350.25, 변화=+1.52%
```

---

## ✅ 체크리스트

### Railway 환경변수
- [ ] `DBSEC_ENABLE=true`
- [ ] `DBSEC_ROUTER_ENABLE=false` (중요!)
- [ ] `DB_APP_KEY=...`
- [ ] `DB_APP_SECRET=...`
- [ ] `K200_CHECK_INTERVAL_MIN=30`
- [ ] `SENTINEL_BASE_URL=...`
- [ ] `TELEGRAM_BOT_TOKEN=...`
- [ ] `TELEGRAM_CHAT_ID=...`

### 배포 확인
- [ ] Health Check 성공
- [ ] Railway 로그에 "K200 선물 감시" 메시지
- [ ] 텔레그램 테스트 알림 수신

### 알림 테스트
- [ ] K200 선물 변동시 알림 (30분 대기)
- [ ] 미국 지수 변동시 알림 (지수 중심)
- [ ] VIX 알림이 S&P/NASDAQ 포함

---

## 🐛 예상 문제 및 해결

### Q1: K200 선물 알림이 안 와요
**확인사항:**
1. Railway 로그에서 "K200 선물 체크" 메시지 확인
2. 변동률 0.8% 이상인지 확인
3. 30분 간격이므로 바로 안 올 수 있음
4. `DBSEC_ENABLE=true` 확인

**해결:**
```bash
# Railway 로그 확인
grep "K200" logs.txt

# 환경변수 재확인
echo $DBSEC_ENABLE
```

### Q2: "DB증권 토큰 발급 실패" 오류
**원인:**
- API 키 오류
- 네트워크 문제

**해결:**
1. DB증권 개발자 센터에서 키 재확인
2. Railway 환경변수 재설정
3. 재배포

### Q3: VIX 알림이 여전히 VIX 중심이에요
**원인:**
- 코드 업데이트 안 됨

**해결:**
```bash
git pull origin main
# Railway 재배포
```

### Q4: 중복 알림이 와요
**원인:**
- DB증권 라우터가 활성화됨

**해결:**
```bash
DBSEC_ROUTER_ENABLE=false  # Railway Variables에서 확인
```

---

## 📊 성능 지표

### API 호출 빈도
- **Yahoo Finance**: 3분마다 (하루 480회)
- **DB증권 API**: 30분마다 (하루 48회)
- **총 API 호출**: 하루 ~530회

### 예상 알림 빈도
- **K200 선물**: 1-3회/일 (거래일)
- **미국 지수**: 1-3회/일
- **VIX 급등락**: 0-2회/일
- **총 알림**: 2-8회/일 (정상 거래일)

### 리소스 사용
- **메모리**: ~100MB
- **CPU**: 낮음 (폴링 방식)
- **네트워크**: 중간 (API 호출)

---

## 🎯 다음 단계 (선택)

### 1. 모니터링 대시보드
- Grafana 연동
- 실시간 차트

### 2. 알림 채널 확장
- Discord 연동
- 이메일 알림
- Slack 통합

### 3. AI 분석 강화
- Caia AI 프롬프트 개선
- 시장 예측 기능 추가

---

## 📞 지원

문제 발생시:
1. Railway 로그 확인
2. 환경변수 재확인
3. GitHub 이슈 등록
4. 텔레그램으로 문의

---

**시스템 버전**: v2.1.0 (K200 통합)  
**최종 업데이트**: 2025-10-11  
**상태**: ✅ 배포 준비 완료  
**GitHub**: https://github.com/projectcaia/FastAPI-Sentinel

---

## 🎉 완료!

이제 Railway에 배포하면:
- ✅ K200 선물 야간 감시 작동
- ✅ 알림 형식 개선 (지수 중심)
- ✅ 시스템 단순화 완료
- ✅ 모든 문제 해결

**Happy Trading! 🚀📊**
