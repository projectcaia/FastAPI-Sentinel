# 📌 DB증권 모듈 상태

## ⚠️ 현재 상태: **비활성화됨 (DISABLED)**

### 비활성화 이유:
1. **API 인증 오류** (IGW00105): "유효하지 않은 AppSecret입니다"
   - DB_APP_KEY 또는 DB_APP_SECRET이 잘못되었거나 만료됨
   
2. **API 호출 한도 초과** (IGW00201): "호출 거래건수를 초과하였습니다"
   - DB증권 API 일일 호출 한도 초과
   - 추가 호출 시 계정이 차단될 수 있음

### 현재 동작:
- ✅ FastAPI 서버는 정상 작동
- ✅ 기존 시장 감시 기능 (market_watcher.py) 정상 작동
- ❌ DB증권 WebSocket 연결 비활성화
- ❌ K200 선물 실시간 모니터링 중단
- ⚠️ Mock 토큰 사용 중 (실제 API 호출 없음)

### 재활성화 방법:

1. **올바른 API 자격 증명 확인**
   ```bash
   # Railway 환경변수에서 확인
   DB_APP_KEY=정확한_앱키_입력
   DB_APP_SECRET=정확한_앱시크릿_입력
   ```

2. **API 한도 확인**
   - DB증권 개발자 포털에서 일일 호출 한도 확인
   - 필요시 한도 증량 신청

3. **모듈 재활성화**
   - `utils/token_manager.py`의 원본 버전 복구
   - `services/dbsec_ws.py`의 원본 버전 복구
   - 서버 재시작

### API 엔드포인트 상태:
- `/sentinel/dbsec/health` - ✅ 작동 (DISABLED 상태 표시)
- `/sentinel/dbsec/stream` - ✅ 작동 (빈 데이터 반환)
- `/sentinel/dbsec/config` - ✅ 작동
- 기타 엔드포인트 - ✅ 작동 (제한된 기능)

### 권장사항:
1. DB증권 개발자 포털에서 API 키 재발급
2. 다음날 (API 한도 리셋 후) 재시도
3. 테스트 시 호출 횟수 제한 (분당 1회 이하)

---

**참고**: 이 비활성화는 임시 조치이며, 올바른 자격 증명과 API 한도가 확보되면 즉시 재활성화 가능합니다.