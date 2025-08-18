# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# 필요한 시스템 패키지 설치
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 의존성
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스
COPY . .

# 상태 파일을 위한 디렉토리 생성
RUN mkdir -p /app/data && chmod 777 /app/data

# 런타임 포트(로컬 기본값 8000)
ENV PORT=8000
ENV WATCHER_STATE_PATH=/app/data/market_state.json

# 컨테이너 "시작" 시에만 서버 실행 (빌드 시 아님)
# env 변수 확장을 위해 sh -c 사용 + PORT 폴백
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
