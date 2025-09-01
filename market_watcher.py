# market_watcher.py — Sentinel 시장감시 워커 (수정 버전)

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict

# -------------------- 설정/로그 --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
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
        log.warning("yfinance 로드 실패: %s", e)

# 한국 지수
KR_SYMBOLS = {
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11"
}

# 미국 지수
US_SYMBOLS = {
    "SPX": "^GSPC",
    "NASDAQ": "^IXIC",
    "VIX": "^VIX",
    "ES": "ES=F",  # S&P500 선물
    "NQ": "NQ=F"   # NASDAQ 선물
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

def is_kr_market_hours() -> bool:
    """한국 정규장 시간 체크 (09:00~15:30)"""
    now = _now_kst()
    if now.weekday() >= 5:  # 주말
        return False
    hhmm = now.hour * 100 + now.minute
    return 900 <= hhmm <= 1530

def is_us_market_hours() -> bool:
    """미국 정규장 시간 체크"""
    now = _now_kst()
    if now.weekday() >= 5:  # 주말
        return False
    h, m = now.hour, now.minute
    is_dst = 3 <= now.month <= 11
    
    if is_dst:  # 서머타임: 22:30~05:00
        if h == 22: return m >= 30
        return 23 <= h or h < 5
    else:  # 표준시: 23:30~06:00
        if h == 23: return m >= 30
        return 0 <= h < 6

def is_us_premarket() -> bool:
    """미국 프리마켓 시간 체크 (정규장 전)"""
    now = _now_kst()
    if now.weekday() >= 5:
        return False
    h = now.hour
    # 대략 17:00~22:30(서머) or 18:00~23:30(표준)
    return 17 <= h <= 22

def get_active_session() -> str:
    """현재 활성 세션 결정"""
    if is_kr_market_hours():
        return "KR"
    elif is_us_market_hours():
        return "US"
    elif is_us_premarket():
        return "US_PRE"
    return "CLOSED"

# -------------------- 상태 관리 --------------------
def _save_state(state: dict):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("상태 저장 실패: %s", e)

def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"levels": {}, "last_prices": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 구조 보장
            if "levels" not in data:
                data["levels"] = {}
            if "last_prices" not in data:
                data["last_prices"] = {}
            return data
    except Exception:
        return {"levels": {}, "last_prices": {}}

# -------------------- 데이터 수집 --------------------
def _http_get(url: str, params=None, timeout=10):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r

def get_yahoo_quote(symbol: str) -> Optional[Dict]:
    """Yahoo Finance에서 실시간 시세 가져오기"""
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        r = _http_get(url, params={"symbols": symbol})
        data = r.json()
        results = data.get("quoteResponse", {}).get("result", [])
        return results[0] if results else None
    except Exception as e:
        log.debug("Yahoo quote 실패 %s: %s", symbol, e)
        return None

def get_yf_data(symbol: str) -> Optional[Tuple[float, float]]:
    """yfinance로 현재가와 전일종가 가져오기"""
    if not _YF_READY:
        return None
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        current = getattr(info, "last_price", None)
        prev = getattr(info, "previous_close", None)
        if current and prev:
            return float(current), float(prev)
    except Exception as e:
        log.debug("yfinance 실패 %s: %s", symbol, e)
    return None

def calculate_change_percent(current: float, previous: float) -> float:
    """변화율 계산"""
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100.0

def get_market_data(symbol: str) -> Optional[Tuple[float, float]]:
    """시장 데이터 수집 (현재가, 변화율)"""
    # Yahoo Finance API 시도
    quote = get_yahoo_quote(symbol)
    if quote:
        # 정규장 데이터 우선
        current = quote.get("regularMarketPrice") or quote.get("price")
        previous = quote.get("regularMarketPreviousClose") or quote.get("previousClose")
        
        if current and previous:
            change = calculate_change_percent(float(current), float(previous))
            return float(current), change
    
    # yfinance 백업
    data = get_yf_data(symbol)
    if data:
        current, previous = data
        change = calculate_change_percent(current, previous)
        return current, change
    
    return None

# -------------------- 레벨 판정 --------------------
def grade_level(change_pct: float, is_vix: bool = False) -> Optional[str]:
    """변화율에 따른 레벨 판정"""
    abs_change = abs(change_pct)
    
    # 0.8% 미만은 무시
    if abs_change < 0.8:
        return None
    
    if is_vix:
        if abs_change >= 10.0: return "LV3"
        if abs_change >= 7.0: return "LV2"
        if abs_change >= 5.0: return "LV1"
        return None
    else:
        if abs_change >= 2.5: return "LV3"
        if abs_change >= 1.5: return "LV2"
        if abs_change >= 0.8: return "LV1"
        return None

# -------------------- 알림 전송 --------------------
def send_alert(symbol: str, change_pct: float, level: str, note: str):
    """센티넬 알림 전송"""
    display_name = DISPLAY_NAMES.get(symbol, symbol)
    
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
            log.info("✓ 알림: %s %s %.2f%% - %s", display_name, level, change_pct, note)
        else:
            log.error("알림 실패: %s", r.text)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# -------------------- 메인 체크 로직 --------------------
def check_markets():
    """시장 체크 및 알림"""
    session = get_active_session()
    
    if session == "CLOSED":
        log.info("시장 마감 시간 - 체크 생략")
        return
    
    log.info("===== 시장 체크 (%s) =====", session)
    
    state = _load_state()
    levels = state["levels"]
    
    # 체크할 심볼 결정
    symbols_to_check = []
    
    if session == "KR":
        symbols_to_check = ["KOSPI", "KOSDAQ"]
    elif session == "US":
        symbols_to_check = ["SPX", "NASDAQ", "VIX"]
    elif session == "US_PRE":
        symbols_to_check = ["ES", "NQ"]  # 선물만
    
    # 각 심볼 체크
    for symbol in symbols_to_check:
        try:
            # 실제 심볼 코드
            actual_symbol = KR_SYMBOLS.get(symbol) or US_SYMBOLS.get(symbol)
            if not actual_symbol:
                continue
            
            # 데이터 수집
            data = get_market_data(actual_symbol)
            if not data:
                log.warning("%s 데이터 수집 실패", symbol)
                continue
            
            current_price, change_pct = data
            
            # VIX 여부 체크
            is_vix = (symbol == "VIX")
            
            # 레벨 판정
            new_level = grade_level(change_pct, is_vix)
            old_level = levels.get(symbol)
            
            log.info("%s: %.2f (%.2f%%) [%s → %s]", 
                     symbol, current_price, change_pct, 
                     old_level or "없음", new_level or "정상")
            
            # 레벨 변경시만 알림
            if new_level != old_level:
                if not old_level and new_level:
                    note = "레벨 진입"
                elif old_level and not new_level:
                    note = "레벨 해제"
                else:
                    note = f"{old_level} → {new_level}"
                
                send_alert(symbol, change_pct, new_level, note)
                levels[symbol] = new_level
            
        except Exception as e:
            log.error("%s 처리 오류: %s", symbol, e)
    
    # 상태 저장
    state["levels"] = levels
    _save_state(state)
    log.info("===== 체크 완료 =====")

# -------------------- 메인 루프 --------------------
def main():
    log.info("=== Sentinel 시장 감시 시작 ===")
    log.info("체크 간격: %d초", WATCH_INTERVAL)
    
    while True:
        try:
            check_markets()
        except Exception as e:
            log.error("체크 실패: %s", e)
        
        time.sleep(WATCH_INTERVAL)

if __name__ == "__main__":
    main()
