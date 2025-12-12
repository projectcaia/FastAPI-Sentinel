# Sentinel 배포 가이드

## 🎯 배포 전 체크리스트

- [ ] PR 생성 및 메인 브랜치 병합 완료
- [ ] Railway 환경변수 설정 완료
- [ ] Cron Job 설정 확인
- [ ] 배포 후 테스트 준비

---

## 📋 Railway 환경변수 설정

### 1️⃣ Sentinel (Main API) 프로젝트

Railway Dashboard → FastAPI-Sentinel 프로젝트 → Variables 탭

**아래 변수들을 Railway Variables에 입력:**

```bash
OPENAI_API_KEY=[현재 사용 중인 OpenAI API 키]
CAIA_ASSISTANT_ID=[현재 사용 중인 Assistant ID]
CAIA_THREAD_ID=[현재 사용 중인 Thread ID]
CAIA_PUSH_MODE=telegram
TELEGRAM_BOT_TOKEN=[현재 사용 중인 Bot Token]
TELEGRAM_CHAT_ID=[현재 사용 중인 Chat ID]
HUB_URL=https://connector-hub-production.up.railway.app/bridge/ingest
CONNECTOR_SECRET=[현재 사용 중인 Connector Secret]
SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app
WATCH_INTERVAL_SEC=1800
WATCHER_STATE_PATH=./market_state.json
DEDUP_WINDOW_MIN=30
USE_PROXY_TICKERS=true
BOLL_K_SIGMA=2.0
BOLL_WINDOW=20
DB_API_BASE=https://openapi.dbsec.co.kr:8443
DB_APP_KEY=[현재 사용 중인 DB API Key]
DB_APP_SECRET=[현재 사용 중인 DB API Secret]
DB_SCOPE=oob
DBSEC_ROUTER_ENABLE=false
LOG_LEVEL=INFO
```

**중요**: 
- `DBSEC_ROUTER_ENABLE=false` - 메인 API에서는 DB증권 라우터 비활성화
- Caia Agent는 Actions GPT로 동작하므로 별도 URL 불필요
- **[현재 사용 중인 ...]** 부분은 기존 Railway Variables에서 복사해서 사용

---

### 2️⃣ Sentinel Worker (Cron Job) 프로젝트

Railway Dashboard → Sentinel Worker 프로젝트 → Variables 탭

**Sentinel (Main API)의 모든 변수 + 아래 Worker 전용 변수 추가:**

```bash
# === 위의 Sentinel Main API 변수 모두 포함 ===

# === Worker 전용 추가 변수 ===
DATA_PROVIDERS=alphavantage,yfinance,yahoo
ALPHAVANTAGE_API_KEY=[현재 사용 중인 AlphaVantage API Key]
YF_ENABLED=true
SEND_MODE=on_change
BRIDGE_MODE=hub
ALIGN_SLOTS=true
DBSEC_ENABLE=true
DB_FUTURES_CODE=101C6000
K200_CHECK_INTERVAL_MIN=30
FORCE_MARKET_OPEN=false
VIX_FILTER_THRESHOLD=0.6
```

**중요**:
- `DBSEC_ENABLE=true` - Worker에서 DB증권 API 활성화
- `DB_FUTURES_CODE=101C6000` - 현재 선물 종목 코드 (분기별 업데이트 필요)
- **[현재 사용 중인 ...]** 부분은 기존 Railway Variables에서 복사해서 사용

---

## 🚀 배포 순서

### Step 1: PR 병합
```bash
# GitHub에서 PR 확인 및 병합
https://github.com/projectcaia/FastAPI-Sentinel/compare/main...genspark_ai_developer
```

### Step 2: Railway 자동 배포 확인
- Railway Dashboard에서 자동 배포 시작 확인
- 빌드 로그 모니터링
- 배포 완료 대기 (약 2-3분)

### Step 3: Cron Job 활성화 확인
Railway Dashboard → Settings → Cron Jobs 탭에서 확인:

```
Name: market-watcher
Schedule: */30 * * * * (매 30분마다)
Command: python market_watcher.py
Status: Active
```

### Step 4: 배포 검증

#### 4.1 API Health Check
```bash
curl https://fastapi-sentinel-production.up.railway.app/health
# 예상 응답: {"status":"ok","version":"..."}
```

#### 4.2 Cron Job 실행 로그 확인
Railway Dashboard → Deployments → Logs에서:
```
✅ Sentinel 시장감시 시작 (Cron Job 단일 실행)
✅ 시장 감시 완료 - 프로세스 종료
```

#### 4.3 알림 테스트
```bash
# 테스트 알림 전송
curl -X POST https://fastapi-sentinel-production.up.railway.app/sentinel/alert \
  -H "Content-Type: application/json" \
  -d '{
    "index": "TEST",
    "symbol": "TEST",
    "level": "INFO",
    "delta_pct": 1.5,
    "triggered_at": "2025-10-16T10:00:00Z",
    "note": "배포 테스트",
    "kind": "INDEX"
  }'
```

확인 사항:
- [ ] Telegram 메시지 수신
- [ ] Caia Thread에 메시지 전송
- [ ] Hub로 전달 성공 로그

---

## 🔧 트러블슈팅

### 문제 1: Cron Job이 실행되지 않음

**확인 사항**:
```bash
# railway.json 파일 존재 확인
ls -la railway.json

# 파일 내용 확인
cat railway.json
```

**해결책**:
- Railway Dashboard에서 수동으로 Cron Job 추가
- Settings → Cron Jobs → Add Cron Job
  - Name: `market-watcher`
  - Schedule: `*/30 * * * *`
  - Command: `python market_watcher.py`

---

### 문제 2: DB증권 API 오류

**확인 사항**:
```bash
# 환경변수 확인
echo $DB_APP_KEY
echo $DB_APP_SECRET
echo $DB_FUTURES_CODE
```

**해결책**:
- `DB_FUTURES_CODE` 분기별 업데이트 확인
  - 2025년 12월물: `101C6000` 
  - 2026년 3월물: `101RC000`
- API 키/시크릿 재확인
- `DBSEC_ENABLE=true` 설정 확인 (Worker만)

---

### 문제 3: 알림이 전송되지 않음

**확인 사항**:
```bash
# 로그 확인
# Railway Dashboard → Logs

# 텔레그램 토큰 확인
echo $TELEGRAM_BOT_TOKEN
echo $TELEGRAM_CHAT_ID

# Hub URL 확인
echo $HUB_URL
echo $CONNECTOR_SECRET
```

**해결책**:
1. Telegram Bot 토큰 재확인
2. Chat ID 재확인 (숫자만)
3. Hub URL 및 시크릿 키 재확인
4. 네트워크 연결 상태 확인

---

### 문제 4: Worker가 종료되지 않음

**증상**: Cron Job이 계속 실행 중

**해결책**:
```python
# market_watcher.py의 run_loop() 함수가 제거되었는지 확인
# check_and_alert_once()만 존재해야 함

# 강제 종료 후 재배포
# Railway Dashboard → Deployments → Force Redeploy
```

---

## 📊 모니터링

### 1. Railway 로그 모니터링
```
Railway Dashboard → Deployments → Logs → Filter: "Sentinel"
```

주요 로그 메시지:
- `✅ Sentinel 시장감시 시작` - Cron 실행 시작
- `✅ 시장 감시 완료` - Cron 실행 종료
- `❌ 시장 감시 오류` - 에러 발생

### 2. Cron 실행 이력
```
Railway Dashboard → Settings → Cron Jobs → Executions
```

확인 항목:
- 실행 횟수 (시간당 2회 = 정상)
- 실행 시간 (30분 간격)
- 성공/실패 상태

### 3. 알림 도달 확인
- Telegram 챗봇 메시지 확인
- Caia Thread 메시지 확인
- Hub 전달 로그 확인

---

## 🔄 롤백 절차

긴급 상황 시 이전 버전으로 롤백:

### Railway Dashboard 롤백
1. Railway Dashboard → Deployments
2. 이전 정상 배포 버전 선택
3. "Redeploy" 버튼 클릭

### GitHub 롤백
```bash
# 이전 커밋으로 롤백
git revert HEAD
git push origin main

# 또는 특정 커밋으로 리셋
git reset --hard <이전-커밋-해시>
git push -f origin main
```

### Cron Job 긴급 비활성화
```bash
# railway.json 수정
{
  "cron": []  # 빈 배열로 설정
}

# 또는 Railway Dashboard에서 수동 비활성화
Settings → Cron Jobs → market-watcher → Disable
```

---

## ✅ 배포 완료 체크리스트

- [ ] PR 병합 완료
- [ ] Railway 자동 배포 성공
- [ ] Sentinel (Main API) 환경변수 설정
- [ ] Sentinel Worker 환경변수 설정
- [ ] Cron Job 활성화 확인
- [ ] Health Check API 응답 확인
- [ ] 첫 번째 Cron 실행 로그 확인 (30분 이내)
- [ ] 테스트 알림 전송 성공
- [ ] Telegram 메시지 수신 확인
- [ ] Caia Thread 메시지 확인
- [ ] Hub 전달 로그 확인
- [ ] DB증권 API 정상 작동 확인 (Worker)
- [ ] 로그 레벨 및 에러 모니터링 설정

---

## 📞 지원

문제 발생 시:
1. `CRON_SETUP_GUIDE.md` 참고
2. Railway 로그 확인
3. GitHub Issues 생성
4. 긴급 시 롤백 실행

**배포 완료 후 24시간 모니터링 권장**
