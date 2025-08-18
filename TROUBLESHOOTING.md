# 🚨 Sentinel 시스템 문제 해결 가이드

## 🔴 긴급 점검 사항 (3시간 동안 알림 없음)

### 1. **Worker 프로세스 확인** (가장 중요!)
```bash
# Railway에서 worker 로그 확인
railway logs --service worker --lines 50

# 확인할 내용:
# - "시장감시 워커 시작" 메시지가 있는지
# - "주기 실행 오류" 메시지가 있는지
# - 최근 로그 시간이 현재 시간과 가까운지
```

**🔍 예상 문제점:**
- Worker 프로세스가 실행되지 않음
- Worker가 크래시됨
- 환경변수 오류로 시작 실패

### 2. **Procfile 확인**
```bash
cat Procfile
```
다음 두 줄이 반드시 있어야 함:
```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
worker: python market_watcher.py
```

### 3. **Railway 프로세스 설정 확인**
Railway 대시보드에서:
1. Settings → Deploy 탭
2. **Processes** 섹션 확인
3. `web`과 `worker` 두 개가 모두 활성화되어 있는지 확인
4. 특히 **worker가 활성화되어 있는지 중요!**

### 4. **환경변수 확인**
Railway Variables에서 다음 확인:

#### 필수 환경변수 (worker용):
```
SENTINEL_BASE_URL=https://[your-app].railway.app
SENTINEL_KEY=[your-key]
WATCH_INTERVAL_SEC=1800
LOG_LEVEL=INFO
```

#### 필수 환경변수 (web용):
```
TELEGRAM_BOT_TOKEN=[your-token]
TELEGRAM_CHAT_ID=[your-chat-id]
OPENAI_API_KEY=sk-[your-key]
CAIA_ASSISTANT_ID=asst_[your-id]
```

### 5. **직접 테스트 알림 전송**
```bash
# API가 작동하는지 직접 테스트
curl -X POST https://[your-app].railway.app/sentinel/alert \
  -H "Content-Type: application/json" \
  -H "x-sentinel-key: [your-sentinel-key]" \
  -d '{
    "index": "TEST",
    "level": "LV2",
    "delta_pct": -1.5,
    "triggered_at": "'$(date -Iseconds)'",
    "note": "수동 테스트"
  }'
```

### 6. **Worker 수동 실행 테스트**
로컬에서 worker를 직접 실행해보기:
```bash
# .env 파일 설정 후
python market_watcher.py
```

## 🔥 즉시 해결 방법

### 방법 1: Worker 재시작
```bash
# Railway CLI에서
railway restart --service worker
```

### 방법 2: 전체 재배포
```bash
railway up
```

### 방법 3: Railway 대시보드에서
1. worker 서비스 선택
2. Settings → Restart 클릭

## 📊 Worker가 정상 작동 중인지 확인하는 방법

정상 로그 예시:
```
2024-XX-XX XX:XX:XX - INFO - 시장감시 워커 시작: interval=1800s
2024-XX-XX XX:XX:XX - INFO - 초기 시장 체크 실행...
2024-XX-XX XX:XX:XX - INFO - KR ΔK200 수집/판정 완료
2024-XX-XX XX:XX:XX - INFO - 알림 전송: ΔK200 LV2 -1.23%
```

비정상 로그 예시:
```
ValueError: invalid literal for int()
ConnectionError: Failed to connect
KeyError: 'SENTINEL_BASE_URL'
```

## 🎯 가장 가능성 높은 원인

1. **Worker 프로세스가 Railway에서 비활성화됨** (90% 확률)
   - Railway는 기본적으로 web만 실행
   - worker를 수동으로 활성화해야 함

2. **SENTINEL_BASE_URL이 잘못 설정됨** (5% 확률)
   - http:// 대신 https:// 사용
   - 끝에 / 없애기

3. **Worker가 크래시 후 재시작 안됨** (5% 확률)
   - Railway 대시보드에서 수동 재시작

## 💡 디버깅 명령어 모음

```bash
# 1. 현재 시장 상태 직접 확인
python test_system.py

# 2. Worker 로그 실시간 확인
railway logs --service worker -f

# 3. Web 로그 실시간 확인  
railway logs --service web -f

# 4. 두 서비스 상태 확인
railway status

# 5. 환경변수 목록 확인
railway variables
```

## 🔔 알림이 오지 않는 체크리스트

- [ ] Worker 프로세스가 실행 중인가?
- [ ] SENTINEL_BASE_URL이 올바른가?
- [ ] TELEGRAM_BOT_TOKEN과 CHAT_ID가 설정되었는가?
- [ ] market_state.json 파일이 생성되었는가?
- [ ] 네트워크 연결이 정상인가?
- [ ] Yahoo Finance API가 정상 응답하는가?
- [ ] 시장 변동이 실제로 LV1 이상인가?

## 📞 추가 지원

위 모든 방법을 시도해도 해결되지 않으면:
1. `railway logs --service worker --lines 100` 전체 로그
2. `railway logs --service web --lines 50` 전체 로그
3. Railway 대시보드 스크린샷 (Processes 섹션)

이 정보와 함께 문의해주세요.