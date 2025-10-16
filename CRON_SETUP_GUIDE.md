# 🔄 Railway Cron Job 설정 가이드

## 📋 개요

Sentinel 시장 감시 시스템을 **Cron Job 기반**으로 전환하여:
- 30분마다 `python market_watcher.py` 실행
- 프로세스가 항상 실행되지 않고 필요할 때만 실행
- 리소스 효율적 운영

---

## 🎯 변경 사항

### 1. market_watcher.py
**변경 전:**
```python
def run_loop():
    while True:
        time.sleep(WATCH_INTERVAL)
        check_and_alert()
```

**변경 후:**
```python
async def check_and_alert_once():
    """한 번만 실행하고 종료"""
    check_and_alert()
    log.info("✅ 시장 감시 완료 - 프로세스 종료")

if __name__ == "__main__":
    asyncio.run(check_and_alert_once())
```

### 2. Procfile
```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
# worker: python market_watcher.py  # Cron으로 전환
```

---

## 🚀 Railway 설정 방법

### Option 1: railway.json 사용 (권장)

프로젝트 루트에 `railway.json` 생성:

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "uvicorn main:app --host 0.0.0.0 --port $PORT",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  },
  "cron": [
    {
      "name": "market-watcher",
      "schedule": "*/30 * * * *",
      "command": "python market_watcher.py"
    }
  ]
}
```

**스케줄 설명:**
- `*/30 * * * *` - 30분마다 실행
- `0 * * * *` - 매시간 정각 실행
- `*/15 * * * *` - 15분마다 실행

### Option 2: Railway 대시보드 설정

1. **Railway 대시보드 접속**
   ```
   https://railway.app
   → sentinel-worker 서비스 선택
   → Settings 탭
   ```

2. **Cron Jobs 섹션**
   ```
   Name: market-watcher
   Schedule: */30 * * * *
   Command: python market_watcher.py
   ```

3. **기존 Worker 프로세스 중단**
   ```
   Deploy → Deployments
   → 기존 worker 프로세스 Stop
   ```

---

## 📊 스케줄 예시

### 30분마다 (권장)
```cron
*/30 * * * *
```
- 00:00, 00:30, 01:00, 01:30, ...

### 15분마다
```cron
*/15 * * * *
```
- 00:00, 00:15, 00:30, 00:45, ...

### 정각마다
```cron
0 * * * *
```
- 00:00, 01:00, 02:00, ...

### 거래 시간대만 (예: 평일 09:00-15:30, 18:00-05:00)
```cron
# 주간: 09:00-15:30 (30분마다)
*/30 9-15 * * 1-5

# 야간: 18:00-23:59 (30분마다)
*/30 18-23 * * 1-5

# 야간: 00:00-05:00 (30분마다)
*/30 0-5 * * 1-5
```

---

## 🔍 동작 확인

### 1. Railway 로그 확인

```bash
railway logs --service sentinel-worker
```

**정상 동작 로그:**
```
Sentinel 시장감시 시작 (Cron Job 단일 실행)
============================================================
시장 체크 시작 [세션: KR] 2025-10-13 10:30:00 KST
【한국 정규장】 데이터 수집 중...
✓ KOSPI: 현재=2543.21, 전일대비=-0.52%
✅ 시장 감시 완료 - 프로세스 종료
```

### 2. Cron 실행 이력 확인

Railway 대시보드:
```
Deployments → Cron Jobs
→ market-watcher 클릭
→ Execution History 확인
```

**확인 항목:**
- ✅ Last Run: 마지막 실행 시간
- ✅ Status: Success / Failed
- ✅ Duration: 실행 소요 시간
- ✅ Logs: 실행 로그

---

## 🐛 문제 해결

### 문제 1: Cron Job이 실행 안 됨

**원인:**
- `railway.json` 문법 오류
- 잘못된 schedule 형식

**해결:**
```bash
# railway.json 검증
cat railway.json | jq .

# Cron 표현식 검증
# https://crontab.guru/
```

### 문제 2: "module not found" 에러

**원인:**
- Python 경로 문제
- 필요한 패키지 미설치

**해결:**
```bash
# requirements.txt 확인
pip install -r requirements.txt

# Railway에서 빌드 로그 확인
railway logs --build
```

### 문제 3: 프로세스가 중단되지 않음

**원인:**
- `check_and_alert_once()`가 `while True` 루프 포함
- `time.sleep()` 호출

**해결:**
```python
# ❌ 잘못된 코드
async def check_and_alert_once():
    while True:  # ← 제거!
        check_and_alert()
        time.sleep(1800)

# ✅ 올바른 코드
async def check_and_alert_once():
    check_and_alert()  # 한 번만 실행
    # 종료 (Cron이 다음 실행 예약)
```

---

## 📈 모니터링

### 1. Cron 실행 통계

**Railway 대시보드:**
```
Metrics → Cron Jobs
→ Execution Count (실행 횟수)
→ Success Rate (성공률)
→ Average Duration (평균 소요 시간)
```

### 2. 알림 전송 확인

**Sentinel API 로그:**
```bash
railway logs --service sentinel | grep "alert"
```

**텔레그램 알림 확인:**
- 30분마다 시장 변동 알림
- 에러 발생 시 즉시 알림

### 3. 상태 파일 확인

```bash
# market_state.json 확인
railway run cat market_state.json | jq .
```

---

## 🔄 롤백 방법

Cron Job으로 전환 후 문제 발생 시:

### 1. Procfile 복원
```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
worker: python market_watcher.py  # ← 다시 활성화
```

### 2. market_watcher.py 복원
```python
# run_loop() 함수 복원
def run_loop():
    while True:
        time.sleep(WATCH_INTERVAL)
        check_and_alert()

if __name__ == "__main__":
    run_loop()
```

### 3. Railway 재배포
```bash
git add Procfile market_watcher.py
git commit -m "revert: Cron Job 롤백"
git push origin main
```

---

## ✅ 체크리스트

### 배포 전
- [ ] `market_watcher.py` 수정 완료 (`check_and_alert_once()`)
- [ ] `Procfile`에서 worker 제거
- [ ] `railway.json` 생성
- [ ] Git commit & push

### 배포 후
- [ ] Railway 대시보드에서 Cron Job 확인
- [ ] 첫 번째 실행 성공 확인 (로그)
- [ ] 30분 후 두 번째 실행 확인
- [ ] 텔레그램 알림 수신 확인
- [ ] 상태 파일 업데이트 확인

### 운영 중
- [ ] 주기적으로 Cron 실행 이력 확인
- [ ] 에러 발생 시 즉시 대응
- [ ] 리소스 사용량 모니터링

---

## 📞 지원

### Railway Cron 문서
- https://docs.railway.app/reference/cron-jobs

### Cron 표현식 테스트
- https://crontab.guru/

### 로그 확인
```bash
# 전체 로그
railway logs --service sentinel-worker

# Cron 관련 로그만
railway logs --service sentinel-worker | grep "Cron"

# 에러 로그만
railway logs --service sentinel-worker | grep "ERROR"
```

---

**작성일:** 2025-10-13  
**버전:** 1.0  
**상태:** ✅ Cron Job 전환 완료

**다음 단계:** Railway 대시보드에서 Cron Job 설정 확인!
