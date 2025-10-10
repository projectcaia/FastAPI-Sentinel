# DB증권 K200 선물 감시 설정 가이드

## 필수 환경 변수

### 1. API 인증 정보
```bash
DB_APP_KEY=your_app_key_here          # DB증권 API 앱 키
DB_APP_SECRET=your_app_secret_here    # DB증권 API 앱 시크릿
```

### 2. 종목 코드 설정
```bash
DB_FUTURES_CODE=101V3000             # K200 선물 종목코드 (중요!)
DB_FUTURES_REST_TR_ID=HHDFS76240000  # REST 현재가 조회 TR ID (선물호가 조회)
DB_FUTURES_TR_ID=H0IFC0              # 실시간 WebSocket TR ID (선물체결 구독)
```

### 3. 알림 민감도 설정 (다른 지표와 동일)
```bash
DB_ALERT_THRESHOLD=1.5    # LV2 이상 알림 임계값 (1.5% 이상)
DB_WARN_THRESHOLD=0.8     # LV1 알림 임계값 (0.8% 이상)
```

### 4. WebSocket 설정
```bash
DB_WS_URL=wss://openapi.dbsec.co.kr:9443/ws               # WebSocket URL
DB_API_BASE=https://openapi.dbsec.co.kr:8443              # REST API URL
DB_FUTURES_QUOTE_PATH=/dfutureoption/quotations/v1/inquire-price  # K200 선물 현재가 조회 경로
DB_FUTURES_HTTP_METHOD=POST                                 # DB증권 REST 명세상의 HTTP 메서드
DB_FUTURES_REQUIRE_HASHKEY=true                             # POST 본문 HashKey 필수 여부
DB_HASHKEY_PATH=/dfutureoption/hashkey                       # HashKey 생성 엔드포인트
```

> 📘 **참고**: DB증권 공식 OpenAPI 명세에 따르면 KOSPI200 선물 현재가 조회는 `POST /dfutureoption/quotations/v1/inquire-price` 엔드포인트와 TR ID `HHDFS76240000`을 사용하며, 본문 HashKey를 반드시 포함해야 합니다. 위 값은 기본값으로 포함되어 있으므로 실제 발급받은 키에 맞춰 변경하세요.

### 5. 모니터링 설정
```bash
DBSEC_ENABLE=true              # DB증권 모듈 활성화
DBSEC_POLL_MINUTES=30          # 재연결 시도 주기 (30분 권장)
DBSEC_WS_SEND_AUTH_HEADER=false # WebSocket 인증 헤더 불필요
LOG_LEVEL=INFO                 # 로그 레벨 (INFO 권장)
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