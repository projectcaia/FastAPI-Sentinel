# DB증권 K200 선물 감시 설정 가이드

## 필수 환경 변수

### 1. API 인증 정보
```bash
DB_APP_KEY=your_app_key_here          # DB증권 API 앱 키
DB_APP_SECRET=your_app_secret_here    # DB증권 API 앱 시크릿
```

### 2. 종목 코드 설정
```bash
DB_FUTURES_CODE=101V3000    # K200 선물 종목코드 (중요!)
DB_FUTURES_TR_ID=HDFSCNT0   # 실시간 TR ID (선물체결 구독)
```

### 3. 알림 민감도 설정
```bash
DB_ALERT_THRESHOLD=0.8    # CRITICAL 알림 임계값 (0.8% 이상)
DB_WARN_THRESHOLD=0.3     # WARNING 알림 임계값 (0.3% 이상)
```

### 4. WebSocket 설정
```bash
DB_WS_URL=wss://openapi.dbsec.co.kr:9443/ws    # WebSocket URL
DB_API_BASE=https://openapi.dbsec.co.kr:8443   # REST API URL
```

### 5. 모니터링 설정
```bash
DBSEC_ENABLE=true              # DB증권 모듈 활성화
DBSEC_POLL_MINUTES=5           # 휴장 시 재확인 주기 (분)
DBSEC_WS_SEND_AUTH_HEADER=true # WebSocket 인증 헤더 포함
```

### 6. Sentinel 통합
```bash
SENTINEL_BASE_URL=https://your-sentinel-url.com  # Sentinel 베이스 URL
SENTINEL_KEY=your_sentinel_key                    # Sentinel API 키
```

## 문제 해결

### 1. WebSocket 연결이 안 되는 경우
- DB_APP_KEY와 DB_APP_SECRET이 올바른지 확인
- 토큰이 만료되지 않았는지 확인 (`/sentinel/dbsec/health` 엔드포인트)
- 네트워크 방화벽이 WebSocket 포트(9443)를 차단하지 않는지 확인

### 2. 실시간 데이터가 오지 않는 경우
- DB_FUTURES_CODE가 올바른 종목코드인지 확인 (K200 선물: 101V3000)
- 현재 거래시간인지 확인 (주간: 09:00-15:30, 야간: 18:00-05:00)
- `/sentinel/dbsec/stream` 엔드포인트로 버퍼 상태 확인

### 3. 알림이 오지 않는 경우
- DB_ALERT_THRESHOLD와 DB_WARN_THRESHOLD 값을 낮춰서 더 민감하게 설정
- SENTINEL_BASE_URL이 올바르게 설정되었는지 확인
- 로그에서 "ANOMALY DETECTED" 메시지가 있는지 확인

### 4. 토큰 관련 오류
- `/sentinel/dbsec/token/refresh` 엔드포인트로 수동 토큰 갱신
- DB증권 API 일일 호출 한도 확인

## 테스트 명령

### Health Check
```bash
curl https://your-app.com/sentinel/dbsec/health
```

### Stream Status
```bash
curl https://your-app.com/sentinel/dbsec/stream
```

### Test Alert
```bash
curl -X POST https://your-app.com/sentinel/dbsec/alert/test
```

### Restart Monitoring
```bash
curl -X POST https://your-app.com/sentinel/dbsec/restart
```

## 로그 확인

정상 작동 시 로그:
```
[DBSEC] Trading session changed from CLOSED to DAY
[DBSEC] Connecting to WebSocket...
[DBSEC] WebSocket connected successfully
[DBSEC] Sent subscribe_msg for K200 Futures
[DBSEC] Parsed tick: price=350.25, change=0.45%, session=DAY
[DBSEC] K200 Futures tick: Price: 350.25, Change: 0.45%
```

알림 발생 시 로그:
```
ANOMALY DETECTED: K200_FUT 0.85% change in DAY session - Level: CRITICAL
Alert sent to MarketWatcher: Level LV2
```