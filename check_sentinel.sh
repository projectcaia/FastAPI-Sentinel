#!/bin/bash
# Sentinel 시스템 긴급 점검 스크립트

echo "🔍 Sentinel 시스템 긴급 진단 시작..."
echo "================================================"

# 1. Railway 서비스 상태 확인
echo ""
echo "1️⃣ Railway 서비스 상태 확인:"
echo "----------------------------------------"
railway status 2>/dev/null || echo "❌ Railway CLI 미설치 또는 미연결"

# 2. 프로세스 확인
echo ""
echo "2️⃣ 실행 중인 프로세스 확인:"
echo "----------------------------------------"
echo "Web 서비스 최근 로그:"
railway logs --service web --lines 10 2>/dev/null || echo "Web 서비스 로그 확인 실패"

echo ""
echo "Worker 서비스 최근 로그:"
railway logs --service worker --lines 20 2>/dev/null || echo "Worker 서비스 로그 확인 실패"

# 3. API 헬스체크
echo ""
echo "3️⃣ API 헬스체크:"
echo "----------------------------------------"
if [ -n "$SENTINEL_BASE_URL" ]; then
    curl -s "$SENTINEL_BASE_URL/health" | python -m json.tool || echo "❌ API 응답 없음"
else
    echo "⚠️ SENTINEL_BASE_URL 환경변수 미설정"
fi

# 4. 현재 시장 데이터 확인
echo ""
echo "4️⃣ 현재 시장 데이터 직접 확인:"
echo "----------------------------------------"
python3 << 'EOF'
import requests
import json
from datetime import datetime, timezone, timedelta

def check_market():
    symbols = ["^KS200", "069500.KS", "102110.KS", "^KS11"]
    
    for symbol in symbols:
        try:
            url = "https://query1.finance.yahoo.com/v7/finance/quote"
            r = requests.get(url, params={"symbols": symbol}, timeout=10)
            data = r.json()
            result = data.get("quoteResponse", {}).get("result", [])
            
            if result:
                quote = result[0]
                change = quote.get("regularMarketChangePercent", 0)
                price = quote.get("regularMarketPrice", 0)
                print(f"  {symbol}: {price:.2f} ({change:+.2f}%)")
                
                if abs(change) >= 1.0:
                    print(f"    ⚠️ LV2 이상 조건 충족!")
        except Exception as e:
            print(f"  {symbol}: 오류 - {e}")

check_market()
EOF

# 5. 상태 파일 확인
echo ""
echo "5️⃣ 상태 파일 확인:"
echo "----------------------------------------"
if [ -f "market_state.json" ]; then
    echo "market_state.json 내용:"
    cat market_state.json
else
    echo "❌ market_state.json 파일 없음"
fi

# 6. 환경변수 확인
echo ""
echo "6️⃣ 중요 환경변수 확인:"
echo "----------------------------------------"
echo "  SENTINEL_BASE_URL: ${SENTINEL_BASE_URL:-NOT SET}"
echo "  TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN:+SET}"
echo "  TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID:-NOT SET}"
echo "  OPENAI_API_KEY: ${OPENAI_API_KEY:+SET}"
echo "  WATCH_INTERVAL_SEC: ${WATCH_INTERVAL_SEC:-NOT SET}"
echo "  DEDUP_WINDOW_MIN: ${DEDUP_WINDOW_MIN:-NOT SET}"

echo ""
echo "================================================"
echo "📋 점검 완료! 위 정보를 확인하세요."