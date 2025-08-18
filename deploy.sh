#!/bin/bash
# Sentinel 시스템 배포 스크립트
# Railway 또는 다른 플랫폼에 배포하기 위한 스크립트

set -e  # 오류 발생시 즉시 중단

echo "🚀 Sentinel 시스템 배포 준비 중..."

# 환경변수 체크
if [ ! -f .env ]; then
    echo "⚠️  .env 파일이 없습니다."
    echo "   .env.example을 복사하여 .env를 만들고 설정해주세요:"
    echo "   cp .env.example .env"
    exit 1
fi

# Git 저장소 확인
if [ ! -d .git ]; then
    echo "📦 Git 저장소 초기화..."
    git init
    git add .
    git commit -m "Initial commit: Sentinel system"
fi

# Railway CLI 설치 확인
if ! command -v railway &> /dev/null; then
    echo "⚠️  Railway CLI가 설치되지 않았습니다."
    echo "   설치 방법: npm install -g @railway/cli"
    echo "   또는: curl -fsSL https://railway.app/install.sh | sh"
    exit 1
fi

# Railway 로그인 확인
if ! railway whoami &> /dev/null; then
    echo "🔐 Railway 로그인이 필요합니다..."
    railway login
fi

# 프로젝트 선택 또는 생성
echo "📂 Railway 프로젝트 설정..."
if [ -f .railway/config.json ]; then
    echo "✅ 기존 Railway 프로젝트 발견"
else
    echo "새 프로젝트를 생성하시겠습니까? (y/n)"
    read -r response
    if [ "$response" = "y" ]; then
        railway init
    else
        railway link
    fi
fi

# 환경변수 설정
echo "⚙️  Railway 환경변수 설정..."
echo "   주의: 민감한 정보는 Railway 대시보드에서 직접 설정하는 것을 권장합니다."

# .env 파일에서 환경변수 읽어서 Railway에 설정
while IFS='=' read -r key value; do
    # 주석과 빈 줄 무시
    if [[ ! "$key" =~ ^# ]] && [[ -n "$key" ]]; then
        # 민감한 정보는 확인 후 설정
        if [[ "$key" =~ (KEY|TOKEN|ID) ]]; then
            echo "   ⚠️  $key 설정을 확인하세요 (민감한 정보)"
        else
            railway variables set "$key=$value" --service web
            railway variables set "$key=$value" --service worker
            echo "   ✅ $key 설정됨"
        fi
    fi
done < .env

# 배포
echo "🚀 Railway에 배포 중..."
railway up

# 배포 상태 확인
echo "📊 배포 상태 확인..."
railway status

# 로그 확인
echo "📝 최근 로그 (web):"
railway logs --service web --lines 20

echo "📝 최근 로그 (worker):"
railway logs --service worker --lines 20

# URL 확인
echo "🌐 배포된 URL:"
railway open

echo "✅ 배포 완료!"
echo ""
echo "다음 단계:"
echo "1. Railway 대시보드에서 환경변수가 올바르게 설정되었는지 확인"
echo "2. 두 개의 서비스가 실행 중인지 확인:"
echo "   - web: FastAPI 서버 (main.py)"
echo "   - worker: 시장 감시 워커 (market_watcher.py)"
echo "3. test_system.py를 실행하여 시스템 테스트:"
echo "   python test_system.py"