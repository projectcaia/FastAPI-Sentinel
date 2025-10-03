# 📌 DB증권 모듈 상태

## ✅ 모듈 활성화 컨트롤 가능

### 환경 변수로 제어:
- `DBSEC_ENABLE=true` → **실제 API 연동** (Production 모드)
- `DBSEC_ENABLE=false` → **Mock 모드** (로컬 개발/테스트용)

### 현재 동작 방식:

#### Production 모드 (DBSEC_ENABLE=true):
- ✅ DB증권 API에서 실제 토큰 획득
- ✅ WebSocket 연결하여 K200 선물 실시간 모니터링
- ✅ 변동률 감지 시 MarketWatcher로 이벤트 전송
- ✅ 23시간마다 자동 토큰 갱신
- ⚠️ **주의**: 올바른 DB_APP_KEY와 DB_APP_SECRET 필요

#### Mock 모드 (DBSEC_ENABLE=false):
- ✅ Mock 토큰 사용 (API 호출 없음)
- ✅ WebSocket 연결 시도하지 않음
- ✅ 서버는 정상 작동하나 DB증권 데이터 없음
- ✅ API 한도 걱정 없이 개발 가능

### Railway 설정:

```bash
# Production (실제 운영)
DBSEC_ENABLE=true
DB_APP_KEY=실제_앱_키
DB_APP_SECRET=실제_앱_시크릿

# Development (개발/테스트)
DBSEC_ENABLE=false
# API 키는 선택사항
```

### API 엔드포인트:
- `/sentinel/dbsec/health` - 모듈 상태 확인
- `/sentinel/dbsec/stream` - 실시간 틱 데이터
- `/sentinel/dbsec/config` - 설정 확인
- `/sentinel/dbsec/sessions` - 거래 세션 정보

### 로그 확인:
시작 시 로그에서 모드 확인 가능:
```
[INFO] [DB증권] Token Manager initialized in PRODUCTION mode
[INFO] [DB증권] K200 Futures monitoring started in PRODUCTION mode
```
또는
```
[INFO] [DB증권] Module DISABLED by DBSEC_ENABLE=false
[INFO] [DB증권] Token Manager initialized in MOCK mode
```

### 문제 해결:

#### API 인증 오류 (IGW00105):
- DB_APP_KEY 또는 DB_APP_SECRET 확인
- DB증권 개발자 포털에서 키 재발급

#### API 한도 초과 (IGW00201):
- 일일 API 호출 한도 초과
- 다음날 재시도 또는 한도 증량 신청
- 임시로 DBSEC_ENABLE=false로 전환 가능

### 권장 설정:
- **Production**: DBSEC_ENABLE=true (실제 데이터 필요 시)
- **Development**: DBSEC_ENABLE=false (개발/테스트 시)
- **API 문제 시**: DBSEC_ENABLE=false (임시 비활성화)

---

**참고**: DBSEC_ENABLE 설정으로 언제든지 실제 모드와 Mock 모드를 전환할 수 있습니다.