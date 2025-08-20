# 🚨 Railway 환경변수 설정 가이드

## ⚠️ 중요: 환경변수 값에 설명을 넣지 마세요!

### ❌ 잘못된 예시
```
DEDUP_WINDOW_MIN = 10 (10분)          # 틀림!
WATCH_INTERVAL_SEC = 1800 (30분)      # 틀림!
BOLL_K_SIGMA = 2.0 (표준편차)         # 틀림!
```

### ✅ 올바른 예시
```
DEDUP_WINDOW_MIN = 30
WATCH_INTERVAL_SEC = 1800
BOLL_K_SIGMA = 2.0
```

## 📋 Railway 환경변수 설정 목록

Railway 대시보드에서 다음과 같이 **숫자만** 입력하세요:

| 변수명 | 권장값 | 설명 (Railway에 입력하지 마세요!) |
|--------|--------|-----------------------------------|
| **OPENAI_API_KEY** | sk-... | OpenAI API 키 |
| **CAIA_ASSISTANT_ID** | asst_... | Caia Assistant ID |
| **CAIA_THREAD_ID** | thread_... | Caia Thread ID |
| **TELEGRAM_BOT_TOKEN** | 123456789:AAA... | 텔레그램 봇 토큰 |
| **TELEGRAM_CHAT_ID** | 123456789 | 텔레그램 채팅 ID |
| **SENTINEL_KEY** | your-random-string | API 보안 키 |
| **SENTINEL_BASE_URL** | https://your-api.railway.app | Sentinel API URL |
| **DEDUP_WINDOW_MIN** | **30** | 중복 알림 억제 시간(분) |
| **WATCH_INTERVAL_SEC** | **1800** | 시장 감시 주기(초) = 30분 |
| **BOLL_K_SIGMA** | **2.5** | 볼린저 밴드 표준편차 (비활성화 상태) |
| **BOLL_WINDOW** | **20** | 볼린저 밴드 이동평균 기간 |
| **LOG_LEVEL** | **INFO** | 로그 레벨 |
| **ALERT_CAP** | **2000** | 알림 버퍼 크기 |
| **WATCHER_STATE_PATH** | **./market_state.json** | 상태 파일 경로 |

## 🔧 Railway에서 설정하는 방법

1. Railway 대시보드 접속
2. 프로젝트 선택
3. Variables 탭 클릭
4. 각 변수를 추가:
   - Key: 변수명 (예: `DEDUP_WINDOW_MIN`)
   - Value: **숫자만** (예: `30`)
   - ⚠️ 괄호나 설명 넣지 마세요!

## 📝 예시 스크린샷 설명

```
KEY                     VALUE
-----------------------------------------
DEDUP_WINDOW_MIN       30              ✅ 올바름
DEDUP_WINDOW_MIN       30 (30분)       ❌ 틀림!
WATCH_INTERVAL_SEC     1800            ✅ 올바름  
WATCH_INTERVAL_SEC     1800초          ❌ 틀림!
BOLL_K_SIGMA          2.0             ✅ 올바름
BOLL_K_SIGMA          2.0σ            ❌ 틀림!
```

## 🚀 변경 후 재배포

환경변수 수정 후:
1. Railway 대시보드에서 자동 재배포 확인
2. 또는 수동으로 Redeploy 클릭
3. 로그 확인:
   ```bash
   railway logs --lines 50
   ```

## ✅ 권장 설정 (안정적인 운영)

- `DEDUP_WINDOW_MIN`: **30** (30분 중복 억제)
- `WATCH_INTERVAL_SEC`: **1800** (30분마다 체크)
- `BOLL_K_SIGMA`: **2.5** (볼린저 밴드 비활성화)
- `BOLL_WINDOW`: **20** (20기간 이동평균)

## 📢 변경된 알림 기준

### 일반 지수 (KOSPI, S&P500, NASDAQ)
- **LV1**: ±0.8% 이상
- **LV2**: ±1.5% 이상
- **LV3**: ±2.5% 이상

### VIX (변동성 지수)
- **LV1**: ±5% 이상
- **LV2**: ±7% 이상
- **LV3**: ±10% 이상
- **스마트 필터**: 지수 변동이 0.8% 미만일 때 VIX 알림 무시

### 볼린저 밴드
- **비활성화**: 노이즈 감소를 위해 일시적 비활성화

이 설정으로 안정적이고 적절한 빈도의 알림을 받을 수 있습니다.