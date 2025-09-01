# market_watcher.py — Sentinel 시장감시 워커 (최종 수정본)
# -*- coding: utf-8 -*-

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict

# -------------------- 설정/로그 --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("market-watcher")

def _normalize_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not (u.startswith("http://") or u.startswith("https://")):
        u = "https://" + u
    return u

SENTINEL_BASE_URL = _normalize_base(os.getenv("SENTINEL_BASE_URL", ""))
SENTINEL_KEY = os.getenv("SENTINEL_KEY", "").strip()

def _pint(key, default):
    import re
    m = re.search(r"\d+", os.getenv(key, str(default)))
    return int(m.group()) if m else default

WATCH_INTERVAL = _pint("WATCH_INTERVAL_SEC", 1800)  # 30분
STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# -------------------- 데이터 소스 --------------------
YF_ENABLED = os.getenv("YF_ENABLED", "true").lower() in ("1","true","yes")
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance load failed: %s", e)

# 심볼 정의
SYMBOLS = {
    # 한국
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
    # 미국 현물
    "SPX": "^GSPC",
    "NASDAQ": "^IXIC",
    "VIX": "^VIX",
    # 미국 선물
    "ES": "ES=F",
    "NQ": "NQ=F"
}

# 표시 이름
DISPLAY_NAMES = {
    "KOSPI": "한국 시장: 코스피",
    "KOSDAQ": "한국 시장: 코스닥",
    "SPX": "미국 S&P500",
    "NASDAQ": "미국 NASDAQ",
    "VIX": "미국 VIX: 변동성지수",
    "ES": "미국 S&P500 선물",
    "NQ": "미국 NASDAQ 선물"
}

# -------------------- 시간 관리 --------------------
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def get_market_session() -> str:
    """현재 활성 시장 판단"""
    now = _now_kst()
    weekday = now.weekday()
    hour = now.hour
    minute = now.minute
    hhmm = hour * 100 + minute
    
    # 주말 체크 (토=5, 일=6)
    if weekday >= 5:
        # 월요일 새벽 선물 체크 (일요일 밤 = 월요일 0시 이후)
        if weekday == 6 and hour >= 18:  # 일요일 18시 이후
            return "US_FUTURES"
        return "CLOSED"
    
    # 평일
    # 한국 정규장: 09:00~15:30
    if 900 <= hhmm <= 1530:
        return "KR"
    
    # 미국 시장 시간 계산 (서머타임 3~11월)
    is_dst = 3 <= now.month <= 11
    
    # 미국 정규장
    if is_dst:  # 서머타임: 22:30~05:00
        if (hour == 22 and minute >= 30) or (23 <= hour) or (hour < 5):
            return "US"
    else:  # 표준시: 23:30~06:00
        if (hour == 23 and minute >= 30) or (0 <= hour < 6):
            return "US"
    
    # 미국 선물 (거의 24시간, 한국 시간 기준 오전 6~7시 정도만 휴장)
    if 7 <= hour <= 22:  # 대략적인 선물 거래 시간
        return "US_FUTURES"
    
    return "CLOSED"

# -------------------- 상태 관리 --------------------
def _save_state(state: dict):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("State save failed: %s", e)

def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"levels": {}, "last_check": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"levels": {}, "last_check": {}}

# -------------------- 데이터 수집 --------------------
def _http_get(url: str, params=None, timeout=10):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        log.debug("HTTP request failed: %s", e)
        raise

def get_yahoo_quote(symbol: str) -> Optional[Dict]:
    """Yahoo Finance API로 시세 조회"""
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        r = _http_get(url, params={"symbols": symbol})
        data = r.json()
        results = data.get("quoteResponse", {}).get("result", [])
        return results[0] if results else None
    except Exception as e:
        log.debug("Yahoo quote failed for %s: %s", symbol, e)
        return None

def get_market_data(symbol_key: str) -> Optional[Tuple[float, float]]:
    """시장 데이터 수집 (현재가, 변화율%)"""
    symbol = SYMBOLS.get(symbol_key)
    if not symbol:
        return None
    
    # Yahoo Finance API
    quote = get_yahoo_quote(symbol)
    if quote:
        # 정규장 우선, 없으면 일반 가격
        current = quote.get("regularMarketPrice") or quote.get("price")
        
        # 한국 시장은 regularMarketPreviousClose 사용
        if symbol_key in ["KOSPI", "KOSDAQ"]:
            previous = quote.get("regularMarketPreviousClose") or quote.get("previousClose")
        else:
            # 미국은 previousClose 사용
            previous = quote.get("previousClose")
        
        if current and previous and previous != 0:
            change_pct = ((float(current) - float(previous)) / float(previous)) * 100
            return float(current), change_pct
    
    # yfinance 백업
    if _YF_READY:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            current = getattr(info, "last_price", None)
            previous = getattr(info, "previous_close", None)
            if current and previous and previous != 0:
                change_pct = ((float(current) - float(previous)) / float(previous)) * 100
                return float(current), change_pct
        except Exception as e:
            log.debug("yfinance failed for %s: %s", symbol, e)
    
    return None

# -------------------- 레벨 판정 --------------------
def calculate_level(change_pct: float, is_vix: bool = False) -> Optional[str]:
    """변화율에 따른 레벨 판정"""
    abs_change = abs(change_pct)
    
    # 0.8% 미만은 무시
    if abs_change < 0.8:
        return None
    
    if is_vix:
        # VIX는 별도 기준
        if abs_change >= 10.0: return "LV3"
        if abs_change >= 7.0: return "LV2"
        if abs_change >= 5.0: return "LV1"
    else:
        # 일반 지수
        if abs_change >= 2.5: return "LV3"
        if abs_change >= 1.5: return "LV2"
        if abs_change >= 0.8: return "LV1"
    
    return None

# -------------------- 알림 전송 --------------------
def send_alert(symbol_key: str, price: float, change_pct: float, level: str, note: str):
    """센티넬로 알림 전송"""
    if not SENTINEL_BASE_URL:
        log.warning("SENTINEL_BASE_URL not configured")
        return
    
    display_name = DISPLAY_NAMES.get(symbol_key, symbol_key)
    
    payload = {
        "index": display_name,
        "level": level or "CLEARED",
        "delta_pct": round(change_pct, 2),
        "triggered_at": _now_kst_iso(),
        "note": note
    }
    
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY
    
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.ok:
            log.info("Alert sent: %s [%s] %.2f%% - %s", 
                    display_name, level, change_pct, note)
        else:
            log.error("Alert failed: %s", r.text)
    except Exception as e:
        log.error("Alert send error: %s", e)

# -------------------- 메인 체크 --------------------
def check_markets():
    """시장 체크 및 알림 처리"""
    session = get_market_session()
    log.info("===== Market Check [%s] =====", session)
    
    if session == "CLOSED":
        log.info("Markets closed - skipping")
        return
    
    # 체크할 심볼 결정
    symbols_to_check = []
    
    if session == "KR":
        symbols_to_check = ["KOSPI", "KOSDAQ"]
    elif session == "US":
        symbols_to_check = ["SPX", "NASDAQ", "VIX"]
    elif session == "US_FUTURES":
        symbols_to_check = ["ES", "NQ"]
    
    # 상태 로드
    state = _load_state()
    levels = state.get("levels", {})
    
    # 각 심볼 체크
    for symbol_key in symbols_to_check:
        try:
            # 데이터 수집
            data = get_market_data(symbol_key)
            if not data:
                log.warning("No data for %s", symbol_key)
                continue
            
            price, change_pct = data
            
            # 레벨 계산
            is_vix = (symbol_key == "VIX")
            new_level = calculate_level(change_pct, is_vix)
            old_level = levels.get(symbol_key)
            
            # 로그
            log.info("%s: $%.2f (%.2f%%) [%s -> %s]",
                    symbol_key, price, change_pct,
                    old_level or "None", new_level or "Normal")
            
            # 레벨 변경시 알림
            if new_level != old_level:
                if not old_level and new_level:
                    note = "레벨 진입"
                elif old_level and not new_level:
                    note = "레벨 해제"
                else:
                    note = f"{old_level} → {new_level}"
                
                send_alert(symbol_key, price, change_pct, new_level, note)
                levels[symbol_key] = new_level
                
        except Exception as e:
            log.error("Error checking %s: %s", symbol_key, e)
    
    # 상태 저장
    state["levels"] = levels
    state["last_check"] = _now_kst_iso()
    _save_state(state)
    
    log.info("===== Check Complete =====")

# -------------------- 메인 루프 --------------------
def main():
    log.info("=== Sentinel Market Watcher Started ===")
    log.info("Check interval: %d seconds", WATCH_INTERVAL)
    log.info("Sentinel URL: %s", SENTINEL_BASE_URL or "NOT SET")
    
    # 초기 체크
    try:
        check_markets()
    except Exception as e:
        log.error("Initial check failed: %s", e)
    
    # 주기적 체크
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            check_markets()
        except Exception as e:
            log.error("Periodic check failed: %s", e)

if __name__ == "__main__":
    main()
