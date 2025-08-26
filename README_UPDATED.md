# 🛡️ Sentinel System - 시장 감시 및 알림 시스템

## 📋 개요
Sentinel은 실시간 시장 감시 시스템으로, 주식시장의 급격한 변동을 감지하고 텔레그램 및 Caia GPT로 알림을 전송합니다.

## 🔧 주요 수정 사항 (2024-08-18)

### 1. **시장 감시 주기 개선**
- ~~30분~~ → **5분** 주기로 변경 (더 빠른 반응)
- 초기 실행 시 즉시 시장 체크 수행
- 주말 감지 로직 추가 (주말은 항상 US 세션)

### 2. **볼린저 밴드 급등/급락 감지**
- ±1.5σ 기준으로 민감도 상향
- BREACH(돌파) / RECOVER(회복) 이벤트 감지
- 레벨 매핑 개선 (LV2로 통합 처리)

### 3. **상태 저장 경로 수정**
- ~~`/mnt/data/`~~ → `./market_state.json` (로컬 디렉토리)
- Docker 환경 지원 (`/app/data/` 마운트)

### 4. **로깅 개선**
- 더 상세한 로그 메시지
- 환경변수 상태 출력
- 알림 전송 성공/실패 로깅

### 5. **중복 알림 억제 개선**
- ~~30분~~ → **10분**으로 단축
- CLEARED 레벨은 항상 전송
- 중복 알림도 inbox에는 저장

## 🚀 빠른 시작

### 1. 환경 설정
```bash
# .env 파일 생성
cp .env.example .env

# .env 파일 편집하여 실제 값 입력
nano .env
```

### 2. 필요한 환경변수
```env
# OpenAI (Caia GPT 연동)
OPENAI_API_KEY=sk-...
CAIA_ASSISTANT_ID=asst_...
CAIA_THREAD_ID=thread_...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Security
SENTINEL_KEY=your-random-string

# Market Watcher
SENTINEL_BASE_URL=https://your-api.railway.app
WATCH_INTERVAL_SEC=300  # 5분
```

### 3. 로컬 테스트
```bash
# 시스템 테스트
python test_system.py

# Docker Compose로 실행
docker-compose up -d

# 로그 확인
docker-compose logs -f
```

### 4. Railway 배포
```bash
# 배포 스크립트 실행
./deploy.sh

# 또는 수동 배포
railway login
railway link
railway up
```

## 📊 시스템 구성

### 컴포넌트
1. **main.py** - FastAPI 서버 (알림 수신/전송)
2. **market_watcher.py** - 시장 감시 워커
3. **test_system.py** - 시스템 테스트 도구

### 알림 레벨

#### 일반 지수 (KOSPI, S&P500, NASDAQ)
- **LV1**: ±0.8% ~ ±1.5% 변동
- **LV2**: ±1.5% ~ ±2.5% 변동
- **LV3**: ±2.5% 이상 변동

#### VIX (변동성 지수) - 스마트 필터 적용
- **LV1**: ±5% ~ ±7% 변동
- **LV2**: ±7% ~ ±10% 변동
- **LV3**: ±10% 이상 변동
- **특징**: S&P500/NASDAQ이 0.8% 미만 변동 시 VIX 알림 무시

#### 볼린저 밴드 (현재 비활성화)
- ~~**BREACH**: 볼린저 밴드 ±2.5σ 돌파~~
- ~~**RECOVER**: 볼린저 밴드 내부로 회복~~
- 현재 노이즈 감소를 위해 비활성화

### 감시 대상
#### KR 세션 (08:30~16:00 KST)
- KOSPI200 (^KS200)
- KODEX200 ETF (069500.KS)
- TIGER200 ETF (102110.KS)
- KOSPI (^KS11)

#### US 세션 - 시장 개장 시간 (KST 22:30~05:00)
- S&P 500 현물 (^GSPC)
- NASDAQ 현물 (^IXIC)
- VIX (^VIX)

#### US 세션 - 장 마감 시간 (KST 05:00~22:30)
- S&P 500 선물 (ES=F)
- NASDAQ 선물 (NQ=F)
- VIX 감시 제외 (장 마감 시 노이즈 방지)

## 🔍 문제 해결

### 알림이 오지 않는 경우
1. **환경변수 확인**
   ```bash
   python test_system.py
   ```

2. **워커 프로세스 확인**
   ```bash
   # Railway
   railway logs --service worker
   
   # Docker
   docker-compose logs worker
   ```

3. **API 서버 상태 확인**
   ```bash
   curl https://your-api.railway.app/health
   ```

### 텔레그램 알림 실패
1. Bot Token과 Chat ID 확인
2. 봇이 채팅방에 추가되었는지 확인
3. 봇 권한 확인 (메시지 전송 권한)

### Caia GPT 연동 실패
1. OpenAI API 키 유효성 확인
2. Assistant ID 확인
3. Thread ID 확인 (ChatGPT 대화창과 동일한지)

## 📝 로그 레벨
- **INFO**: 일반 작동 로그
- **WARNING**: 경고 (시스템은 계속 작동)
- **ERROR**: 오류 (일부 기능 실패)

## 🛠️ 유지보수

### 로그 확인
```bash
# Railway
railway logs --lines 100

# Docker
docker-compose logs -f --tail=100

# 특정 서비스만
docker-compose logs -f worker
```

### 상태 파일 초기화
```bash
# 레벨 상태 초기화
rm market_state.json
# 또는
echo "{}" > market_state.json
```

### 서비스 재시작
```bash
# Docker
docker-compose restart

# Railway
railway restart
```

## 📚 추가 개선 계획
- [ ] 웹 대시보드 추가
- [ ] 과거 알림 이력 조회 API
- [ ] 더 많은 기술적 지표 추가
- [ ] 알림 우선순위 설정
- [ ] 사용자별 알림 설정

## 🤝 지원
문제가 지속되면 다음 정보와 함께 문의:
1. `test_system.py` 실행 결과
2. 최근 로그 (worker, web 모두)
3. 환경변수 설정 상태 (민감한 정보 제외)

### 🆕 KR 선물 우선 감시
- 환경변수 `KR_FUT_SYMBOLS` (예: `K200=F,KOSPI200=F`)를 설정하면 정규장 ΔK200 산출 시 선물 Δ를 우선 사용합니다.
- 실패 시 `^KS200` → `069500.KS, 102110.KS` 평균 → `^KS11` 순으로 폴백합니다.
