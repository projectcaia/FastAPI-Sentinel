# Sentinel Requirements Fix (2025-08-25)

이 패키지는 센티넬 컨테이너가 `uvicorn` 미설치로 기동 실패하는 문제를 고치기 위한 최소 교체본입니다.

## 포함 파일
- `requirements.txt` — FastAPI/uvicorn/pydantic 고정 버전

## 적용 방법 (Railway + Dockerfile 기준)
1) 이 zip을 풀고, `requirements.txt`를 **리포 루트**에 덮어씌웁니다.
2) GitHub에 커밋/푸시합니다. (Railway가 자동 빌드/배포)
3) Railway Start Command가 아래와 같은지 확인합니다.
   ```
   uvicorn app.main:app --host 0.0.0.0 --port 8080
   ```
4) 배포 후 Logs에서 `Uvicorn running on http://0.0.0.0:8080` 문구가 보이면 정상입니다.

## 주의
- 센티넬 코드는 건드리지 않습니다. (라우트/텔레그램 로직 그대로 유지)
- 워커는 별도의 restore-pack으로 복구하세요.
