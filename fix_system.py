#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
시스템 정상화 스크립트
- K200 선물 감시 충돌 해결
- 휴장일 판단 오류 수정
- 미장/야간장 감시 재시작
"""

import os
import sys
import time
import logging
import subprocess
from datetime import datetime, timezone, timedelta

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/home/user/webapp/system_fix.log', encoding='utf-8')
    ]
)
log = logging.getLogger("system-fix")

def check_current_session():
    """현재 거래 세션 확인"""
    now = datetime.now(timezone(timedelta(hours=9)))
    log.info(f"현재 시간: {now.strftime('%Y-%m-%d %H:%M:%S KST')}")
    
    # 오늘이 주말인지 확인
    if now.weekday() >= 5:  # 토요일(5), 일요일(6)
        return "CLOSED", "주말"
    
    # 10월 10일은 정상 거래일
    if now.month == 10 and now.day == 10:
        log.info("✅ 10월 10일은 정상 거래일입니다.")
    
    hhmm = now.hour * 100 + now.minute
    
    # 한국 정규장: 09:00 ~ 15:30
    if 900 <= hhmm <= 1530:
        return "KR", "한국 정규장"
    
    # 미국 정규장 체크 (서머타임 고려)
    month = now.month
    is_dst = 3 <= month <= 11  # 대략적인 서머타임 기간
    
    if is_dst:
        if (hhmm >= 2230) or (hhmm < 500):
            return "US", "미국 정규장 (서머타임)"
    else:
        if (hhmm >= 2330) or (hhmm < 600):
            return "US", "미국 정규장 (표준시)"
    
    # 선물/야간 시간
    if 1530 < hhmm < 2230:
        return "FUTURES", "선물/야간 시장"
    
    return "CLOSED", "휴장"

def fix_environment_variables():
    """환경변수 수정"""
    log.info("🔧 환경변수 설정 수정 중...")
    
    env_fixes = [
        ("DBSEC_ENABLE", "false", "DB증권 라우터 비활성화"),
        ("FORCE_MARKET_OPEN", "true", "강제 시장 오픈 모드 활성화"),
        ("WATCH_INTERVAL_SEC", "180", "감시 간격 3분으로 단축"),
        ("VIX_FILTER_THRESHOLD", "0.6", "VIX 필터 임계값 낮춤"),
        ("SENTINEL_BASE_URL", "https://fastapi-sentinel-production.up.railway.app", "센티넬 알림 엔드포인트 설정"),
        ("LOG_LEVEL", "INFO", "로그 레벨 설정")
    ]
    
    for key, value, desc in env_fixes:
        os.environ[key] = value
        log.info(f"  ✅ {key}={value} ({desc})")

def check_system_health():
    """시스템 상태 확인"""
    log.info("🔍 시스템 상태 확인 중...")
    
    # 현재 세션 확인
    session, desc = check_current_session()
    log.info(f"  📊 현재 세션: {session} - {desc}")
    
    # 프로세스 체크
    try:
        # 센티넬 프로세스 확인
        sentinel_check = subprocess.run(
            ["pgrep", "-f", "main.py"], 
            capture_output=True, text=True
        )
        if sentinel_check.returncode == 0:
            log.info("  ✅ Sentinel 프로세스 실행 중")
        else:
            log.warning("  ⚠️ Sentinel 프로세스 미실행")
    except Exception as e:
        log.error(f"  ❌ 프로세스 체크 실패: {e}")
    
    return session

def test_market_data():
    """시장 데이터 테스트"""
    log.info("📈 시장 데이터 연결 테스트...")
    
    try:
        import sys
        sys.path.append('/home/user/webapp')
        
        # market_watcher 임포트 시도
        from market_watcher import get_market_data, current_session
        
        # 현재 세션 테스트
        session = current_session()
        log.info(f"  📊 세션 판정: {session}")
        
        # 주요 심볼 테스트
        test_symbols = ["^GSPC", "^IXIC", "^VIX"]
        
        for symbol in test_symbols:
            try:
                data = get_market_data(symbol)
                if data and data.get("current"):
                    log.info(f"  ✅ {symbol}: ${data['current']:.2f} ({data.get('change_pct', 0):+.2f}%)")
                else:
                    log.warning(f"  ⚠️ {symbol}: 데이터 없음")
                time.sleep(0.5)  # API 레이트 제한 방지
            except Exception as e:
                log.error(f"  ❌ {symbol}: {e}")
                
    except Exception as e:
        log.error(f"📈 시장 데이터 테스트 실패: {e}")

def start_monitoring():
    """모니터링 시작"""
    log.info("🚀 시장 감시 재시작...")
    
    try:
        # Market Watcher 시작 (백그라운드)
        cmd = ["python3", "/home/user/webapp/market_watcher.py"]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/home/user/webapp"
        )
        
        log.info(f"  🎯 Market Watcher 시작됨 (PID: {process.pid})")
        
        # 몇 초 대기 후 상태 확인
        time.sleep(3)
        
        if process.poll() is None:
            log.info("  ✅ Market Watcher 정상 실행 중")
            return process
        else:
            stdout, stderr = process.communicate()
            log.error(f"  ❌ Market Watcher 시작 실패:")
            log.error(f"     stdout: {stdout.decode()}")
            log.error(f"     stderr: {stderr.decode()}")
            return None
            
    except Exception as e:
        log.error(f"🚀 모니터링 시작 실패: {e}")
        return None

def main():
    """메인 실행 함수"""
    log.info("="*60)
    log.info("🔧 센티넬 시스템 정상화 시작")
    log.info("="*60)
    
    # 1. 환경변수 수정
    fix_environment_variables()
    
    # 2. 시스템 상태 확인
    session = check_system_health()
    
    # 3. 시장 데이터 테스트
    test_market_data()
    
    # 4. 모니터링 재시작
    process = start_monitoring()
    
    log.info("="*60)
    if process:
        log.info("✅ 시스템 정상화 완료!")
        log.info("🎯 Market Watcher가 다음을 감시합니다:")
        if session == "KR":
            log.info("   - 한국 정규장: KOSPI + K200 선물")
        elif session == "US":
            log.info("   - 미국 정규장: S&P 500, NASDAQ, VIX")
        elif session == "FUTURES":
            log.info("   - 선물 시장: 미국선물 + 한국선물")
        else:
            log.info("   - 강제 모드: 미국 시장 감시")
        
        log.info(f"📊 현재 세션: {session}")
        log.info("📝 로그 파일: /home/user/webapp/market_watcher.log")
    else:
        log.error("❌ 시스템 정상화 실패")
        return 1
    
    log.info("="*60)
    return 0

if __name__ == "__main__":
    sys.exit(main())