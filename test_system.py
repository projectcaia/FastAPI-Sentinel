#!/usr/bin/env python3
"""
Sentinel 시스템 테스트 스크립트
시스템 전체 구성요소를 점검하고 문제점을 진단합니다.
"""

import os
import sys
import json
import requests
import logging
from datetime import datetime, timezone, timedelta

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("test_system")

def test_env_vars():
    """환경변수 설정 확인"""
    log.info("=" * 60)
    log.info("환경변수 체크:")
    
    required_vars = {
        "OPENAI_API_KEY": "OpenAI API 키",
        "CAIA_ASSISTANT_ID": "Caia Assistant ID", 
        "CAIA_THREAD_ID": "Caia Thread ID",
        "TELEGRAM_BOT_TOKEN": "텔레그램 봇 토큰",
        "TELEGRAM_CHAT_ID": "텔레그램 채팅 ID",
        "SENTINEL_KEY": "Sentinel 보안 키",
        "SENTINEL_BASE_URL": "Sentinel API URL"
    }
    
    missing = []
    for var, desc in required_vars.items():
        value = os.getenv(var, "")
        if value:
            # 민감한 정보는 일부만 표시
            if "KEY" in var or "TOKEN" in var:
                display = f"{value[:10]}..." if len(value) > 10 else "SET"
            else:
                display = value
            log.info(f"  ✅ {var}: {display}")
        else:
            log.warning(f"  ❌ {var}: NOT SET ({desc})")
            missing.append(var)
    
    return len(missing) == 0

def test_market_data():
    """시장 데이터 수집 테스트"""
    log.info("=" * 60)
    log.info("시장 데이터 수집 테스트:")
    
    # Yahoo Finance API 테스트
    symbols = ["^KS200", "069500.KS", "102110.KS", "^KS11", "^GSPC", "^IXIC", "^VIX"]
    
    for symbol in symbols:
        try:
            url = "https://query1.finance.yahoo.com/v7/finance/quote"
            r = requests.get(
                url, 
                params={"symbols": symbol},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            
            result = data.get("quoteResponse", {}).get("result", [])
            if result:
                quote = result[0]
                price = quote.get("regularMarketPrice", "N/A")
                change = quote.get("regularMarketChangePercent", "N/A")
                log.info(f"  ✅ {symbol}: 가격={price}, 변동률={change}%")
            else:
                log.warning(f"  ⚠️  {symbol}: 데이터 없음")
        except Exception as e:
            log.error(f"  ❌ {symbol}: 오류 - {e}")
    
    return True

def test_telegram():
    """텔레그램 연결 테스트"""
    log.info("=" * 60)
    log.info("텔레그램 연결 테스트:")
    
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    
    if not (token and chat_id):
        log.warning("  ❌ 텔레그램 환경변수 미설정")
        return False
    
    try:
        # getMe API로 봇 정보 확인
        url = f"https://api.telegram.org/bot{token}/getMe"
        r = requests.get(url, timeout=10)
        
        if r.ok:
            bot_info = r.json().get("result", {})
            log.info(f"  ✅ 봇 이름: {bot_info.get('username', 'Unknown')}")
            
            # 테스트 메시지 전송
            test_msg = f"🔍 Sentinel 시스템 테스트\n시간: {datetime.now().isoformat()}"
            msg_url = f"https://api.telegram.org/bot{token}/sendMessage"
            r2 = requests.post(
                msg_url,
                data={"chat_id": chat_id, "text": test_msg},
                timeout=10
            )
            
            if r2.ok:
                log.info("  ✅ 테스트 메시지 전송 성공")
                return True
            else:
                log.error(f"  ❌ 메시지 전송 실패: {r2.text}")
        else:
            log.error(f"  ❌ 봇 정보 조회 실패: {r.text}")
    except Exception as e:
        log.error(f"  ❌ 텔레그램 테스트 실패: {e}")
    
    return False

def test_openai():
    """OpenAI API 연결 테스트"""
    log.info("=" * 60)
    log.info("OpenAI API 연결 테스트:")
    
    api_key = os.getenv("OPENAI_API_KEY", "")
    assistant_id = os.getenv("CAIA_ASSISTANT_ID", "")
    thread_id = os.getenv("CAIA_THREAD_ID", "")
    
    if not api_key:
        log.warning("  ❌ OpenAI API 키 미설정")
        return False
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "assistants=v2"
    }
    
    try:
        # Assistant 확인
        if assistant_id:
            url = f"https://api.openai.com/v1/assistants/{assistant_id}"
            r = requests.get(url, headers=headers, timeout=10)
            
            if r.ok:
                asst = r.json()
                log.info(f"  ✅ Assistant 이름: {asst.get('name', 'Unknown')}")
            else:
                log.warning(f"  ⚠️  Assistant 조회 실패: {r.status_code}")
        
        # Thread 확인
        if thread_id:
            url = f"https://api.openai.com/v1/threads/{thread_id}"
            r = requests.get(url, headers=headers, timeout=10)
            
            if r.ok:
                log.info(f"  ✅ Thread ID 유효: {thread_id[:20]}...")
            else:
                log.warning(f"  ⚠️  Thread 조회 실패: {r.status_code}")
        
        return True
        
    except Exception as e:
        log.error(f"  ❌ OpenAI API 테스트 실패: {e}")
        return False

def test_sentinel_api():
    """Sentinel API 엔드포인트 테스트"""
    log.info("=" * 60)
    log.info("Sentinel API 테스트:")
    
    base_url = os.getenv("SENTINEL_BASE_URL", "http://localhost:8000")
    sentinel_key = os.getenv("SENTINEL_KEY", "")
    
    try:
        # Health 체크
        health_url = f"{base_url}/health"
        r = requests.get(health_url, timeout=10)
        
        if r.ok:
            health = r.json()
            log.info(f"  ✅ API 상태: {health.get('status', 'unknown')}")
            log.info(f"  ✅ 버전: {health.get('version', 'unknown')}")
            log.info(f"  ✅ Alert 버퍼 크기: {health.get('alert_buf_len', 0)}/{health.get('alert_cap', 0)}")
        else:
            log.warning(f"  ⚠️  Health 체크 실패: {r.status_code}")
        
        # 테스트 알림 전송
        alert_url = f"{base_url}/sentinel/alert"
        headers = {"Content-Type": "application/json"}
        if sentinel_key:
            headers["x-sentinel-key"] = sentinel_key
        
        test_data = {
            "index": "TEST",
            "level": "LV1",
            "delta_pct": -1.23,
            "triggered_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
            "note": "시스템 테스트 알림"
        }
        
        r = requests.post(alert_url, json=test_data, headers=headers, timeout=10)
        
        if r.ok:
            result = r.json()
            log.info(f"  ✅ 테스트 알림 전송: {result}")
        else:
            log.warning(f"  ⚠️  알림 전송 실패: {r.status_code} - {r.text}")
        
        return r.ok
        
    except requests.exceptions.ConnectionError:
        log.error(f"  ❌ API 서버에 연결할 수 없음: {base_url}")
        log.info("  💡 서버가 실행 중인지 확인하세요")
    except Exception as e:
        log.error(f"  ❌ API 테스트 실패: {e}")
    
    return False

def main():
    """메인 테스트 실행"""
    log.info("🚀 Sentinel 시스템 종합 테스트 시작")
    log.info("=" * 60)
    
    # .env 파일 로드 시도
    try:
        from dotenv import load_dotenv
        if load_dotenv():
            log.info("✅ .env 파일 로드 성공")
        else:
            log.info("⚠️  .env 파일 없음 - 시스템 환경변수 사용")
    except ImportError:
        log.info("⚠️  python-dotenv 미설치 - 시스템 환경변수만 사용")
    
    # 각 컴포넌트 테스트
    results = {
        "환경변수": test_env_vars(),
        "시장데이터": test_market_data(),
        "텔레그램": test_telegram(),
        "OpenAI": test_openai(),
        "Sentinel API": test_sentinel_api()
    }
    
    # 결과 요약
    log.info("=" * 60)
    log.info("📊 테스트 결과 요약:")
    
    for component, success in results.items():
        status = "✅ 정상" if success else "❌ 문제 있음"
        log.info(f"  {component}: {status}")
    
    # 전체 상태
    all_ok = all(results.values())
    log.info("=" * 60)
    
    if all_ok:
        log.info("🎉 모든 테스트 통과! 시스템이 정상적으로 작동할 준비가 되었습니다.")
    else:
        log.warning("⚠️  일부 문제가 발견되었습니다. 위의 로그를 확인하세요.")
        log.info("\n💡 권장 조치:")
        
        if not results["환경변수"]:
            log.info("  1. .env 파일을 생성하고 필요한 환경변수를 설정하세요")
            log.info("     cp .env.example .env")
            log.info("     그 다음 .env 파일을 편집하여 실제 값을 입력하세요")
        
        if not results["Sentinel API"]:
            log.info("  2. Sentinel API 서버를 시작하세요:")
            log.info("     python main.py  # 또는 uvicorn main:app")
        
        if not results["텔레그램"]:
            log.info("  3. 텔레그램 봇 토큰과 채팅 ID를 확인하세요")
        
        if not results["OpenAI"]:
            log.info("  4. OpenAI API 키와 Assistant 설정을 확인하세요")
    
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())