# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# 의존성
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스
COPY . .

# 런타임 포트(로컬 기본값 8000)
ENV PORT=8000

# 컨테이너 "시작" 시에만 서버 실행 (빌드 시 아님)
# env 변수 확장을 위해 sh -c 사용 + PORT 폴백
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
