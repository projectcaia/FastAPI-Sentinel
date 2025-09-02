# market_watcher.py — FGPT Sentinel 시장감시 워커 (실시간 변동 감지)
# -*- coding: utf-8 -*-

import os, time, json, logging, requests, math
from datetime import datetime, timezone, timedelta
from collections import deque

# ==================== 설정/로그 ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('market_watcher.log', encoding='utf-8')
    ]
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

def parse_int_env(key: str, default: int) -> int:
    value = os.getenv(key, str(default))
    import re
    m = re.search(r'\d+', value)
    return int(m.group()) if m else default

def parse_float_env(key: str, default: float) -> float:
    value = os.getenv(key, str(default))
    import re
    m = re.search(r'[\d.]+', value)
    try:
        return float(m.group()) if m else default
    except Exception:
        return default

# 주기/설정
WATCH_INTERVAL = parse_int_env("WATCH_INTERVAL_SEC", 300)  # 5분 (더 자주 체크)
VIX_FILTER_THRESHOLD = parse_float_env("VIX_FILTER_THRESHOLD", 0.8)
FORCE_ALERT_INTERVAL = parse_int_env("FORCE_ALERT_HOURS", 4)
VOLATILITY_WINDOW = parse_int_env("VOLATILITY_WINDOW_MIN", 60)  # 60분 윈도우

STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# ==================== 심볼/표기 ====================
HUMAN_NAMES = {
    "^KS200":     "KOSPI 200",
    "^KS11":      "KOSPI",
    "069500.KS":  "KODEX 200",
    "102110.KS":  "TIGER 200",
    "^GSPC":      "S&P 500",
    "^IXIC":      "NASDAQ",
    "^VIX":       "VIX",
    "ES=F":       "S&P 500 선물",
    "NQ=F":       "NASDAQ-100 선물",
}

def human_name(sym: str) -> str:
    return HUMAN_NAMES.get(sym, sym)

# 심볼 정의
KR_SPOT_PRIORITY = ["^KS11", "069500.KS", "102110.KS", "^KS200"]
US_SPOT = ["^GSPC", "^IXIC", "^VIX"]
FUTURES_SYMBOLS = ["ES=F", "NQ=F"]

# ==================== 시간 유틸 ====================
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def _utc_ts_now() -> float:
    return datetime.now(timezone.utc).timestamp()

# ==================== 상태 파일 ====================
def _save_state(state: dict):
    """상태 저장"""
    try:
        temp_path = STATE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        
        if os.path.exists(temp_path):
            os.replace(temp_path, STATE_PATH)
            log.debug("상태 저장 완료")
    except Exception as e:
        log.error("상태 저장 실패: %s", e)

def _load_state() -> dict:
    """상태 로드"""
    if not os.path.exists(STATE_PATH):
        log.info("상태 파일 없음 - 새로 생성")
        return {"price_history": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
            # price_history가 없으면 추가
            if "price_history" not in state:
                state["price_history"] = {}
            return state
    except Exception as e:
        log.error("상태 로드 실패: %s - 초기화", e)
        return {"price_history": {}}

# ==================== HTTP 유틸 ====================
H_COMMON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def _http_get(url: str, params=None, timeout=10, max_retry=2):
    last = None
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=H_COMMON, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            time.sleep(0.5 + i)
            last = e
            continue
    if last:
        raise last
    raise RuntimeError("HTTP 요청 실패")

# ==================== 데이터 수집 (개선) ====================
def get_market_data(symbol: str) -> dict | None:
    """시장 데이터 수집 - 현재가, 시가, 전일종가, 일중 고저"""
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {
            "symbols": symbol,
            "fields": "regularMarketPrice,regularMarketOpen,regularMarketPreviousClose,regularMarketDayHigh,regularMarketDayLow,regularMarketChangePercent",
            "crumb": str(int(time.time()))
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            q = items[0]
            return {
                "current": q.get("regularMarketPrice"),
                "open": q.get("regularMarketOpen"),
                "prev_close": q.get("regularMarketPreviousClose"),
                "high": q.get("regularMarketDayHigh"),
                "low": q.get("regularMarketDayLow"),
                "change_pct": q.get("regularMarketChangePercent"),
                "timestamp": time.time()
            }
    except Exception as e:
        log.debug("Quote API 실패(%s): %s", symbol, e)
    
    # Chart API 폴백
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "1m", "range": "1d"}
        
        r = _http_get(url, params=params)
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        indicators = chart.get("indicators", {}).get("quote", [{}])[0]
        
        current = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        
        # 시가, 고가, 저가 계산
        opens = [o for o in (indicators.get("open") or []) if o is not None]
        highs = [h for h in (indicators.get("high") or []) if h is not None]
        lows = [l for l in (indicators.get("low") or []) if l is not None]
        
        open_price = opens[0] if opens else None
        high = max(highs) if highs else None
        low = min(lows) if lows else None
        
        if current and prev_close:
            change_pct = ((current - prev_close) / prev_close) * 100
            
            return {
                "current": current,
                "open": open_price,
                "prev_close": prev_close,
                "high": high,
                "low": low,
                "change_pct": change_pct,
                "timestamp": time.time()
            }
    except Exception as e:
        log.debug("Chart API 실패(%s): %s", symbol, e)
    
    return None

# ==================== 변동성 계산 ====================
def calculate_volatility(state: dict, symbol: str, current_price: float) -> dict:
    """실시간 변동성 계산"""
    
    # 가격 히스토리 관리
    if "price_history" not in state:
        state["price_history"] = {}
    
    if symbol not in state["price_history"]:
        state["price_history"][symbol] = []
    
    history = state["price_history"][symbol]
    now = time.time()
    
    # 현재 가격 추가
    history.append({"price": current_price, "time": now})
    
    # 오래된 데이터 제거 (60분 윈도우)
    cutoff = now - (VOLATILITY_WINDOW * 60)
    history = [h for h in history if h["time"] > cutoff]
    state["price_history"][symbol] = history
    
    if len(history) < 2:
        return {"max_swing": 0, "current_swing": 0}
    
    prices = [h["price"] for h in history]
    
    # 최근 60분 내 최고/최저
    recent_high = max(prices)
    recent_low = min(prices)
    
    # 최대 변동폭 (고점에서 저점까지)
    max_swing = ((recent_high - recent_low) / recent_low) * 100 if recent_low > 0 else 0
    
    # 현재 위치 (저점 대비)
    current_swing = ((current_price - recent_low) / recent_low) * 100 if recent_low > 0 else 0
    
    return {
        "max_swing": max_swing,
        "current_swing": current_swing,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "samples": len(history)
    }

# ==================== 레벨 판정 ====================
def grade_level(value: float, is_vix: bool = False, is_volatility: bool = False) -> str | None:
    """레벨 판정"""
    a = abs(value)
    
    if is_vix:
        # VIX 레벨
        if a >= 25.0: return "LV3"
        if a >= 15.0: return "LV2"
        if a >= 8.0:  return "LV1"
    elif is_volatility:
        # 변동성 기반 (일중 스윙)
        if a >= 3.0: return "LV3"
        if a >= 2.0: return "LV2"
        if a >= 1.0: return "LV1"
    else:
        # 일반 지수 (전일 대비)
        if a >= 2.5: return "LV3"
        if a >= 1.5: return "LV2"
        if a >= 0.8: return "LV1"
    return None

# ==================== 알림 전송 ====================
def post_alert(data: dict, level: str | None, symbol: str, note: str, kind: str = "ALERT"):
    """알림 전송"""
    display_name = human_name(symbol)
    
    payload = {
        "index": display_name,
        "level": level or "INFO",
        "delta_pct": round(data.get("change_pct", 0), 2),
        "triggered_at": _now_kst_iso(),
        "note": note,
        "kind": kind,
        "symbol": symbol,
        "details": {
            "current": data.get("current"),
            "high": data.get("high"),
            "low": data.get("low"),
            "volatility": data.get("volatility", {})
        }
    }
    
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY
    
    if not SENTINEL_BASE_URL:
        log.warning("SENTINEL_BASE_URL 미설정 - 알림 스킵")
        return
    
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            log.error("알림 전송 실패 %s %s", r.status_code, r.text)
        else:
            log.info(">>> 알림 전송: [%s] %s %s (%s)", 
                    kind, display_name, level or "INFO", note)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# ==================== 시장 시간 판정 ====================
def current_session() -> str:
    """현재 세션 판정"""
    now = _now_kst()
    
    # 주말
    if now.weekday() >= 5:
        return "CLOSED"
    
    # 한국 공휴일 (간단 체크 - 필요시 확장)
    kr_holidays = [
        (1, 1),   # 신정
        (3, 1),   # 삼일절
        (5, 5),   # 어린이날
        (6, 6),   # 현충일
        (8, 15),  # 광복절
        (10, 3),  # 개천절
        (10, 9),  # 한글날
        (12, 25), # 크리스마스
    ]
    
    if (now.month, now.day) in kr_holidays:
        log.info("한국 공휴일 감지")
    
    hhmm = now.hour * 100 + now.minute
    
    # 한국 정규장: 09:00 ~ 15:30
    if 900 <= hhmm <= 1530:
        return "KR"
    
    # 미국 정규장 (서머타임): 22:30 ~ 05:00
    # 미국 정규장 (표준시): 23:30 ~ 06:00
    month = now.month
    is_dst = 3 <= month <= 11
    
    if is_dst:
        if (hhmm >= 2230) or (hhmm < 500):
            return "US"
    else:
        if (hhmm >= 2330) or (hhmm < 600):
            return "US"
    
    # 선물 시간
    if 1530 < hhmm < 2230:
        return "FUTURES"
    
    return "CLOSED"

# ==================== 메인 감시 로직 ====================
def check_and_alert():
    """메인 감시 로직 - 실시간 변동성 감지"""
    state = _load_state()
    sess = current_session()
    
    log.info("="*60)
    log.info("시장 체크 시작 [세션: %s] %s", sess, _now_kst().strftime("%Y-%m-%d %H:%M:%S KST"))
    log.info("="*60)
    
    state["last_checked_at"] = _now_kst_iso()
    state["last_session"] = sess
    
    # 강제 알림 체크
    last_alert_time = state.get("last_alert_time", 0)
    current_time = time.time()
    force_alert = (current_time - last_alert_time) > (FORCE_ALERT_INTERVAL * 3600)
    
    if sess == "CLOSED":
        log.info("시장 휴장 중 - 감시 스킵")
        _save_state(state)
        return
    
    # 세션별 심볼 선택
    if sess == "KR":
        symbols = KR_SPOT_PRIORITY[:1]  # KOSPI만
        session_name = "한국 정규장"
    elif sess == "US":
        symbols = US_SPOT
        session_name = "미국 정규장"
    elif sess == "FUTURES":
        symbols = FUTURES_SYMBOLS
        session_name = "선물 시장"
    else:
        _save_state(state)
        return
    
    log.info("【%s】 데이터 수집 중...", session_name)
    
    # VIX 필터용 지수 변동 체크 (미국장만)
    max_index_move = 0
    if sess == "US":
        for sym in ["^GSPC", "^IXIC"]:
            data = get_market_data(sym)
            if data and data.get("change_pct"):
                max_index_move = max(max_index_move, abs(data["change_pct"]))
    
    # 각 심볼 감시
    for symbol in symbols:
        data = get_market_data(symbol)
        if not data or data.get("current") is None:
            log.warning("⚠ %s 데이터 수집 실패", human_name(symbol))
            continue
        
        name = human_name(symbol)
        is_vix = (symbol == "^VIX")
        
        # VIX 필터
        if is_vix and sess == "US" and max_index_move < VIX_FILTER_THRESHOLD:
            log.info("VIX 필터: 지수 변동 %.2f%% < %.1f%% → VIX 무시", 
                    max_index_move, VIX_FILTER_THRESHOLD)
            continue
        
        # 변동성 계산
        volatility = calculate_volatility(state, symbol, data["current"])
        data["volatility"] = volatility
        
        # 로그 출력
        log.info("✓ %s: 현재=%.2f, 전일대비=%.2f%%, 일중변동=%.2f%% (고:%.2f/저:%.2f)", 
                name, 
                data["current"],
                data.get("change_pct", 0),
                volatility.get("max_swing", 0),
                data.get("high", 0),
                data.get("low", 0))
        
        # 상태 키
        state_key = f"{sess}_{symbol}"
        prev_state = state.get(state_key, {})
        
        # 레벨 판정 (다중 기준)
        change_level = grade_level(data.get("change_pct", 0), is_vix=is_vix)
        volatility_level = grade_level(volatility.get("max_swing", 0), is_volatility=True)
        
        # 최종 레벨 (더 높은 것 선택)
        levels = [l for l in [change_level, volatility_level] if l]
        if levels:
            level_order = {"LV1": 1, "LV2": 2, "LV3": 3}
            current_level = max(levels, key=lambda x: level_order.get(x, 0))
        else:
            current_level = None
        
        prev_level = prev_state.get("level")
        prev_volatility = prev_state.get("volatility", {}).get("max_swing", 0)
        
        # 알림 조건
        should_alert = False
        alert_note = ""
        
        # 1. 레벨 변경
        if current_level != prev_level:
            should_alert = True
            if not prev_level and current_level:
                alert_note = f"{current_level} 진입"
            elif prev_level and not current_level:
                alert_note = "정상 복귀"
            else:
                alert_note = f"{prev_level} → {current_level}"
            
            # 변동성 정보 추가
            if volatility.get("max_swing", 0) >= 1.0:
                alert_note += f" (일중 {volatility['max_swing']:.1f}% 변동)"
        
        # 2. 급격한 변동성 증가
        elif volatility.get("max_swing", 0) - prev_volatility >= 1.0:
            should_alert = True
            alert_note = f"변동성 급증: {prev_volatility:.1f}% → {volatility['max_swing']:.1f}%"
        
        # 3. 강제 알림
        elif force_alert and current_level:
            should_alert = True
            alert_note = f"정기: {current_level} 유지 중"
        
        # 4. 선물 특별 처리
        elif sess == "FUTURES" and abs(data.get("change_pct", 0)) >= 0.8:
            if symbol not in state.get("futures_alerted", {}):
                should_alert = True
                alert_note = f"선물 {'상승' if data['change_pct'] > 0 else '하락'} {abs(data['change_pct']):.2f}%"
                if "futures_alerted" not in state:
                    state["futures_alerted"] = {}
                state["futures_alerted"][symbol] = current_time
        
        if should_alert:
            post_alert(data, current_level, symbol, alert_note, kind=sess)
            state["last_alert_time"] = current_time
        
        # 상태 업데이트
        state[state_key] = {
            "level": current_level,
            "change_pct": data.get("change_pct"),
            "volatility": volatility,
            "updated_at": _now_kst_iso()
        }
    
    _save_state(state)
    log.info("체크 완료")
    log.info("-"*60)

# ==================== 메인 루프 ====================
def run_loop():
    log.info("="*60)
    log.info("Sentinel 시장감시 시작 (실시간 변동성 모드)")
    log.info("="*60)
    log.info("설정:")
    log.info("  - 체크 간격: %d초", WATCH_INTERVAL)
    log.info("  - 전일대비: 0.8% / 1.5% / 2.5%")
    log.info("  - 일중변동: 1.0% / 2.0% / 3.0%")
    log.info("  - VIX: 8% / 15% / 25%")
    log.info("  - 변동성 윈도우: %d분", VOLATILITY_WINDOW)
    log.info("-"*60)
    
    # 초기 체크
    try:
        check_and_alert()
    except Exception as e:
        log.error("초기 체크 실패: %s", e, exc_info=True)
    
    # 주기적 체크
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            check_and_alert()
        except Exception as e:
            log.error("주기 체크 오류: %s", e, exc_info=True)

if __name__ == "__main__":
    run_loop()
