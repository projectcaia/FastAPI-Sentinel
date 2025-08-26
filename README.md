# Sentinel Hub Patch Bundle (v2025-08-26)

## 목적
센티넬 알람(`/sentinel/alert`)을 기존 텔레그램 발송과 동시에 **Connector Hub**(`/bridge/ingest`)로 전송하기 위함.

## 설치
1. 레포에 `app/hub_patch.py` 추가.
2. `main.py`에 아래 2줄 삽입:
```python
from hub_patch import register_hub_forwarder
register_hub_forwarder(app)
```
3. 환경변수 세팅:
```
HUB_URL=https://<허브도메인>.up.railway.app/bridge/ingest
CONNECTOR_SECRET=sentinel_20250818_abcd1234
```
4. 재배포 후 `/jobs`에서 알람 이벤트 확인.

## 테스트
- 센티넬 알람 발생 → 텔레그램 정상 발송.
- 동시에 Hub 로그에 `POST /bridge/ingest 200` 기록.
- `/jobs`에서 이벤트 조회 가능.

## 롤백
- `main.py`의 두 줄 제거, `hub_patch.py` 삭제 → 기존 센티넬 기능만 유지.
