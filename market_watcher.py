# market_watcher.py — FGPT Sentinel 시장감시 워커 (실시간 수정 버전)
# -*- coding: utf-8 -*-

import os, time, json, logging, requests, math
from datetime import datetime, timezone, timedelta

# ==================== 설정/로그 ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
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

# 주기/신선도
WATCH_INTERVAL = parse_int_env("WATCH_INTERVAL_SEC", 1800)  # 30분
VIX_FILTER_THRESHOLD = parse_float_env("VIX_FILTER_THRESHOLD", 0.8)  # VIX 필터 임계값

STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# yfinance 설정
YF_ENABLED = os.getenv("YF_ENABLED", "true").lower() in ("1", "true", "yes")
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance import 실패: %s", e)

# ==================== 심볼/표기 ====================
HUMAN_NAMES = {
    "^KS200":     "KOSPI 200",
    "^KS11":      "KOSPI",
    "069500.KS":  "KODEX 200",
    "102110.KS":  "TIGER 200",
    "^GSPC":      "S&P 500",
    "^IXIC":      "NASDAQ",
    "^VIX":       "VIX 변동성지수",
    "ES=F":       "S&P 500 선물",
    "NQ=F":       "NASDAQ-100 선물",
}

def human_name(sym: str) -> str:
    return HUMAN_NAMES.get(sym, sym)

# 심볼 정의
KR_SPOT_PRIORITY = ["069500.KS", "102110.KS", "^KS11"]  
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
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("상태 저장 실패: %s", e)

def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# ==================== HTTP 유틸 ====================
H_COMMON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
}

def _http_get(url: str, params=None, timeout=12, max_retry=3):
    last = None
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=H_COMMON, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            time.sleep(1.0 + i)
            last = e
            continue
    if last:
        raise last
    raise RuntimeError("HTTP 요청 실패")

# ==================== 데이터 수집 (단순화) ====================
def get_realtime_data(symbol: str) -> tuple[float | None, str]:
    """실시간 데이터 수집 - yfinance 우선"""
    
    # 1차: yfinance (가장 신뢰성 높음)
    if _YF_READY:
        try:
            ticker = yf.download(symbol, period="1d", interval="1m", progress=False, prepost=True)
            if ticker is not None and len(ticker) > 0:
                current = float(ticker["Close"].iloc[-1])
                
                # 전일 종가 가져오기
                info = yf.Ticker(symbol).info
                prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
                
                if not prev:
                    # 2일치 데이터로 전일 종가 추출
                    daily = yf.download(symbol, period="2d", interval="1d", progress=False)
                    if daily is not None and len(daily) >= 2:
                        prev = float(daily["Close"].iloc[-2])
                
                if prev and prev != 0:
                    change_pct = (current - prev) / prev * 100.0
                    log.debug("%s yfinance: 현재=%.2f, 전일=%.2f, 변화=%.2f%%", 
                            symbol, current, prev, change_pct)
                    return change_pct, "yfinance"
        except Exception as e:
            log.debug("yfinance 실패(%s): %s", symbol, e)
    
    # 2차: Yahoo Quote API
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {
            "symbols": symbol,
            "crumb": str(int(time.time()))
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            q = items[0]
            
            # regularMarketChangePercent 직접 사용
            change_pct = q.get("regularMarketChangePercent")
            if change_pct is not None:
                log.debug("%s Yahoo Quote: %.2f%%", symbol, change_pct)
                return float(change_pct), "yahoo_quote"
            
            # 계산
            price = q.get("regularMarketPrice")
            prev = q.get("regularMarketPreviousClose")
            if price and prev and prev != 0:
                change_pct = (float(price) - float(prev)) / float(prev) * 100.0
                log.debug("%s Yahoo 계산: %.2f%%", symbol, change_pct)
                return change_pct, "yahoo_calc"
                
    except Exception as e:
        log.debug("Yahoo Quote 실패(%s): %s", symbol, e)
    
    return None, "failed"

def get_futures_data(symbol: str) -> tuple[float | None, str]:
    """선물 데이터 수집 - Chart API 메타 우선"""
    
    # Chart API (선물은 이게 제일 정확)
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "interval": "5m",
            "range": "1d"
        }
        
        r = _http_get(url, params=params)
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        
        current = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        
        if current and prev and prev != 0:
            change_pct = (float(current) - float(prev)) / float(prev) * 100.0
            log.debug("선물 %s: %.2f%%", symbol, change_pct)
            return change_pct, "chart"
            
    except Exception as e:
        log.debug("선물 Chart 실패(%s): %s", symbol, e)
    
    # 폴백: 일반 데이터 수집
    return get_realtime_data(symbol)

# ==================== 시장 시간 판정 ====================
def current_session() -> str:
    """현재 세션 판정"""
    now = _now_kst()
    
    # 주말
    if now.weekday() >= 5:
        return "CLOSED"
    
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

# ==================== 레벨 판정 ====================
def grade_level(delta_pct: float, is_vix: bool = False) -> str | None:
    a = abs(delta_pct)
    
    if is_vix:
        # VIX 레벨 (높게 설정)
        if a >= 25.0: return "LV3"
        if a >= 15.0: return "LV2"
        if a >= 8.0:  return "LV1"
    else:
        # 일반 지수
        if a >= 2.5: return "LV3"
        if a >= 1.5: return "LV2"
        if a >= 0.8: return "LV1"
    return None

# ==================== 알림 전송 ====================
def post_alert(delta_pct: float | None, level: str | None, source_tag: str, note: str, kind: str = "ALERT"):
    display_name = human_name(source_tag)
    payload = {
        "index": display_name,
        "level": level or "CLEARED",
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": note,
        "kind": kind,
        "symbol": source_tag,
    }
    
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY
    
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if not r.ok:
            log.error("알림 전송 실패 %s %s", r.status_code, r.text)
        else:
            log.info("알림 전송: [%s] %s %s %.2f%% (%s)", 
                    kind, display_name, level or "CLEARED", delta_pct or 0, note)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# ==================== 메인 감시 로직 ====================
def check_and_alert():
    state = _load_state()
    sess = current_session()
    
    log.info("===== 시장 체크 [%s] %s =====", sess, _now_kst().strftime("%Y-%m-%d %H:%M:%S KST"))
    state["last_checked_at"] = _now_kst_iso()
    
    if sess == "CLOSED":
        log.info("시장 휴장 - 감시 스킵")
        _save_state(state)
        return
    
    if sess == "KR":
        # 한국 정규장
        log.info("한국 정규장 데이터 수집...")
        
        for sym in KR_SPOT_PRIORITY:
            delta, source = get_realtime_data(sym)
            if delta is not None:
                lvl = grade_level(delta)
                prev = state.get("ΔK200")
                name = human_name(sym)
                
                log.info("%s: %.2f%% [%s → %s] (소스: %s)", 
                        name, delta, prev or "없음", lvl or "정상", source)
                
                if lvl != prev:
                    note = ("레벨 진입" if (not prev and lvl) else
                           "레벨 해제" if (prev and not lvl) else
                           f"{prev} → {lvl}")
                    post_alert(delta, lvl, sym, note)
                    state["ΔK200"] = lvl
                break
        else:
            log.warning("한국 시장 데이터 수집 실패")
    
    elif sess == "US":
        # 미국 정규장
        log.info("미국 정규장 데이터 수집...")
        
        # S&P, NASDAQ 먼저 수집
        spx_delta, _ = get_realtime_data("^GSPC")
        ndx_delta, _ = get_realtime_data("^IXIC")
        
        max_index_move = 0.0
        if spx_delta is not None and ndx_delta is not None:
            max_index_move = max(abs(spx_delta), abs(ndx_delta))
            log.info("지수 변동: S&P %.2f%%, NASDAQ %.2f%%", spx_delta, ndx_delta)
        
        for sym in US_SPOT:
            delta, source = get_realtime_data(sym)
            if delta is None:
                log.warning("%s 데이터 수집 실패", human_name(sym))
                continue
            
            is_vix = (sym == "^VIX")
            
            # VIX 필터
            if is_vix and max_index_move < VIX_FILTER_THRESHOLD:
                log.info("VIX 필터: 지수 %.2f%% < %.2f%% → VIX %.2f%% 무시", 
                        max_index_move, VIX_FILTER_THRESHOLD, delta)
                state["ΔVIX"] = None
                continue
            
            lvl = grade_level(delta, is_vix=is_vix)
            key = "ΔVIX" if is_vix else ("ΔSPX" if sym == "^GSPC" else "ΔNASDAQ")
            prev = state.get(key)
            name = human_name(sym)
            
            log.info("%s: %.2f%% [%s → %s]", name, delta, prev or "없음", lvl or "정상")
            
            if lvl != prev:
                note = ("레벨 진입" if (not prev and lvl) else
                       "레벨 해제" if (prev and not lvl) else
                       f"{prev} → {lvl}")
                if is_vix and lvl and spx_delta and ndx_delta:
                    note += f" (S&P {spx_delta:+.2f}%, NAS {ndx_delta:+.2f}%)"
                post_alert(delta, lvl, sym, note)
                state[key] = lvl
    
    elif sess == "FUTURES":
        # 선물 시간
        log.info("선물 시장 데이터 수집...")
        
        for key, sym in [("ΔES_FUT", "ES=F"), ("ΔNQ_FUT", "NQ=F")]:
            delta, source = get_futures_data(sym)
            name = human_name(sym)
            
            if delta is None:
                log.info("%s: 데이터 없음", name)
                state[key] = None
                continue
            
            log.info("%s: %.2f%% (소스: %s)", name, delta, source)
            
            # 0.8% 미만은 무시
            if abs(delta) < 0.8:
                log.info("%s: %.2f%% (0.8%% 미만 → 알림 생략)", name, delta)
                state[key] = delta
                continue
            
            # 노이즈 억제
            prev_val = state.get(key)
            if prev_val is not None:
                try:
                    if abs(delta - float(prev_val)) < 0.1:
                        log.info("%s: %.2f%% (변화 미미 → 알림 생략)", name, delta)
                        state[key] = delta
                        continue
                except:
                    pass
            
            note = f"선물 시장 변동"
            post_alert(delta, "PRE", sym, note, kind="PRE")
            log.info("%s: %.2f%% [PRE 알림]", name, delta)
            state[key] = delta
    
    _save_state(state)
    log.info("===== 체크 완료 =====")

# ==================== 메인 루프 ====================
def run_loop():
    log.info("=== Sentinel 시장감시 시작 ===")
    log.info("간격: %d초", WATCH_INTERVAL)
    log.info("임계값 - 일반: 0.8%/1.5%/2.5%, VIX: 8%/15%/25%")
    log.info("VIX 필터: 지수 변동 %.1f%% 미만시 무시", VIX_FILTER_THRESHOLD)
    
    # 초기 체크
    try:
        check_and_alert()
    except Exception as e:
        log.error("초기 체크 실패: %s", e)
    
    # 주기적 체크
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            check_and_alert()
        except Exception as e:
            log.error("주기 체크 오류: %s", e)

if __name__ == "__main__":
    run_loop()
