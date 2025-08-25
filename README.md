# OpenAI → Caia 메인 대화창 Thread로 메시지 푸시
OPENAI_API_KEY=sk-...
CAIA_THREAD_ID=thread_xxxxxxxxxxxxxxxxx

# Telegram → 동현 개인 알림
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=123456789

# 보안/운영
SENTINEL_KEY=use-a-long-random-string
DEDUP_WINDOW_MIN=30
LOG_LEVEL=INFO

## 🚀 Market Watcher Worker (자동 시장감시)

- 파일: `market_watcher.py`
- 역할: ^KS200 → (069500.KS, 102110.KS) → ^KS11 순서로 ΔK200(%) 추정, LV 등급 판정 후 `/sentinel/alert` 자동 호출
- 실행: Railway `worker` 프로세스로 실행 (`Procfile`에 추가됨)

### 환경변수(.env)
```
SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app
SENTINEL_KEY=change_this_to_a_long_random_string   # (선택) main.py와 동일하면 됨
WATCH_INTERVAL_SEC=30
LOG_LEVEL=INFO
```

### 배포 체크리스트
1) `requirements.txt`에 추가 설치 불필요(기존 requests 사용)
2) `Procfile`에 `worker: python market_watcher.py` 라인 확인
3) Railway에서 Processes에 `worker`가 뜨는지 확인, 로그 확인
4) `/sentinel/alert` 응답이 `delivered`로 나오면 성공
```


