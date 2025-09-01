# market_watcher.py — FGPT Sentinel 시장감시 워커 (안정화 버전)
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
MAX_STALENESS_SEC = parse_int_env("MAX_STALENESS_SEC", 1800)  # 30분 이내 데이터만 허용
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
KR_SPOT_PRIORITY = ["069500.KS", "102110.KS", "^KS11"]  # ETF 우선 (더 안정적)
US_SPOT = ["^GSPC", "^IXIC", "^VIX"]
ES_FUT, NQ_FUT = "ES=F", "NQ=F"

# ==================== 시간 유틸 ====================
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def _utc_ts_now() -> float:
    return datetime.now(timezone.utc).timestamp()

def _utc_to_kst(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(timezone(timedelta(hours=9)))

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

# ==================== Yahoo Finance API ====================
def _yahoo_quote(symbols):
    symbols_param = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": symbols_param})
    return r.json()

def _extract_current_data(q: dict):
    """현재 시장 데이터 추출 (정규장/프리/포스트 모두 고려)"""
    # 시장 상태 확인
    market_state = q.get("marketState", "")
    
    # 현재가와 타임스탬프 추출 (우선순위: regular > post > pre)
    price = None
    timestamp = None
    
    # 정규장 데이터
    if market_state == "REGULAR" or q.get("regularMarketPrice"):
        price = q.get("regularMarketPrice")
        timestamp = q.get("regularMarketTime")
    
    # 시간외 데이터 (정규장 데이터가 없거나 오래된 경우)
    if price is None or (timestamp and (_utc_ts_now() - float(timestamp)) > MAX_STALENESS_SEC):
        # Post-market
        if q.get("postMarketPrice"):
            post_ts = q.get("postMarketTime")
            if post_ts and (_utc_ts_now() - float(post_ts)) <= MAX_STALENESS_SEC:
                price = q.get("postMarketPrice")
                timestamp = post_ts
        
        # Pre-market
        if price is None and q.get("preMarketPrice"):
            pre_ts = q.get("preMarketTime")
            if pre_ts and (_utc_ts_now() - float(pre_ts)) <= MAX_STALENESS_SEC:
                price = q.get("preMarketPrice")
                timestamp = pre_ts
    
    # 전일 종가
    prev = q.get("regularMarketPreviousClose") or q.get("previousClose")
    
    try:
        price = float(price) if price else None
        prev = float(prev) if prev else None
        timestamp = float(timestamp) if timestamp else None
    except:
        pass
    
    return price, prev, timestamp

def _calculate_change_percent(q: dict) -> tuple[float | None, bool]:
    """변화율 계산 및 신선도 체크"""
    price, prev, timestamp = _extract_current_data(q)
    
    # 신선도 체크
    is_fresh = False
    if timestamp:
        age_sec = _utc_ts_now() - timestamp
        is_fresh = age_sec <= MAX_STALENESS_SEC
        if not is_fresh:
            log.debug("데이터 오래됨: %d초 경과", age_sec)
    
    # 변화율 계산
    if price and prev and prev != 0:
        change_pct = (price - prev) / prev * 100.0
        return change_pct, is_fresh
    
    # 백업: regularMarketChangePercent 사용
    cp = q.get("regularMarketChangePercent")
    if cp is not None:
        try:
            return float(cp), is_fresh
        except:
            pass
    
    return None, False

# ==================== yfinance 보조 ====================
def _yf_get_current_data(symbol: str) -> tuple[float | None, bool]:
    """yfinance로 현재 데이터 가져오기"""
    if not _YF_READY:
        return None, False
    
    try:
        ticker = yf.Ticker(symbol)
        
        # 최근 1일 1분봉 데이터
        df = ticker.history(period="1d", interval="1m", prepost=True, actions=False)
        if df is not None and len(df) > 0:
            # 마지막 캔들 확인
            last_time = df.index[-1]
            last_close = float(df["Close"].iloc[-1])
            
            # 신선도 체크
            now = datetime.now(timezone.utc)
            age = (now - last_time.tz_localize('UTC')).total_seconds()
            if age <= MAX_STALENESS_SEC:
                # 전일 종가 가져오기
                info = ticker.info
                prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
                if prev and prev != 0:
                    change_pct = (last_close - prev) / prev * 100.0
                    return change_pct, True
        
        # 백업: fast_info 사용
        fast = ticker.fast_info
        if hasattr(fast, 'last_price') and hasattr(fast, 'previous_close'):
            if fast.previous_close and fast.previous_close != 0:
                change_pct = (fast.last_price - fast.previous_close) / fast.previous_close * 100.0
                return change_pct, True
                
    except Exception as e:
        log.debug("yfinance 실패(%s): %s", symbol, e)
    
    return None, False

# ==================== 선물 데이터 수집 ====================
def get_futures_data(symbol: str) -> tuple[float | None, bool]:
    """선물 데이터 수집 (야간용)"""
    # 1차: Yahoo API
    try:
        data = _yahoo_quote([symbol])
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            change_pct, is_fresh = _calculate_change_percent(items[0])
            if change_pct is not None and is_fresh:
                return change_pct, True
    except Exception as e:
        log.debug("선물 Yahoo 실패(%s): %s", symbol, e)
    
    # 2차: yfinance
    return _yf_get_current_data(symbol)

# ==================== 현물 데이터 수집 ====================
def get_spot_data(symbols) -> tuple[float | None, str]:
    """현물 데이터 수집 (우선순위대로 시도)"""
    if not isinstance(symbols, list):
        symbols = [symbols]
    
    for sym in symbols:
        # 1차: Yahoo API
        try:
            data = _yahoo_quote([sym])
            items = data.get("quoteResponse", {}).get("result", [])
            if items:
                change_pct, is_fresh = _calculate_change_percent(items[0])
                if change_pct is not None and is_fresh:
                    return change_pct, sym
        except Exception as e:
            log.debug("Yahoo 실패(%s): %s", sym, e)
        
        # 2차: yfinance
        change_pct, is_fresh = _yf_get_current_data(sym)
        if change_pct is not None and is_fresh:
            return change_pct, sym
    
    return None, ""

# ==================== 시장 시간 판정 ====================
def is_market_holiday() -> bool:
    """주말/휴일 체크"""
    now = _now_kst()
    # 주말
    if now.weekday() >= 5:
        return True
    # TODO: 한국/미국 공휴일 체크 추가 가능
    return False

def current_session() -> str:
    """현재 세션 판정"""
    if is_market_holiday():
        return "CLOSED"
    
    now = _now_kst()
    hhmm = now.hour * 100 + now.minute
    
    # 한국 정규장: 09:00 ~ 15:30
    if 900 <= hhmm <= 1530:
        return "KR"
    
    # 미국 정규장 (서머타임 기준): 22:30 ~ 05:00
    # 겨울: 23:30 ~ 06:00
    month = now.month
    is_dst = 3 <= month <= 11
    
    if is_dst:
        if (hhmm >= 2230) or (hhmm < 500):
            return "US"
    else:
        if (hhmm >= 2330) or (hhmm < 600):
            return "US"
    
    # 그 외: 선물 시간 (미국 정규장 전)
    if 1530 < hhmm < 2230:
        return "FUTURES"
    
    return "CLOSED"

# ==================== 레벨 판정 ====================
def grade_level(delta_pct: float, is_vix: bool = False) -> str | None:
    a = abs(delta_pct)
    if is_vix:
        if a >= 10.0: return "LV3"
        if a >= 7.0:  return "LV2"
        if a >= 5.0:  return "LV1"
    else:
        if a >= 2.5:  return "LV3"
        if a >= 1.5:  return "LV2"
        if a >= 0.8:  return "LV1"
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
    
    log.info("===== 시장 체크 [%s] =====", sess)
    state["last_checked_at"] = _now_kst_iso()
    
    if sess == "CLOSED":
        log.info("시장 휴장 - 감시 스킵")
        _save_state(state)
        return
    
    if sess == "KR":
        # 한국 정규장
        delta, tag = get_spot_data(KR_SPOT_PRIORITY)
        if delta is None:
            log.warning("KR 데이터 수집 실패 - 신선한 데이터 없음")
            return
        
        lvl = grade_level(delta)
        prev = state.get("ΔK200")
        name = human_name(tag)
        
        log.info("%s: %.2f%% [%s → %s]", name, delta, prev or "없음", lvl or "정상")
        
        if lvl != prev:
            note = ("레벨 진입" if (not prev and lvl) else
                   "레벨 해제" if (prev and not lvl) else
                   f"{prev} → {lvl}")
            post_alert(delta, lvl, tag, note)
            state["ΔK200"] = lvl
    
    elif sess == "US":
        # 미국 정규장
        # S&P, NASDAQ 먼저 수집 (VIX 필터용)
        spx_delta, _ = get_spot_data(["^GSPC"])
        ndx_delta, _ = get_spot_data(["^IXIC"])
        
        for sym in US_SPOT:
            delta, _ = get_spot_data([sym])
            if delta is None:
                log.warning("%s 데이터 수집 실패", human_name(sym))
                continue
            
            is_vix = (sym == "^VIX")
            
            # VIX 필터: 지수 변동이 작으면 무시
            if is_vix and spx_delta and ndx_delta:
                max_move = max(abs(spx_delta), abs(ndx_delta))
                if max_move < VIX_FILTER_THRESHOLD:
                    log.debug("VIX 필터: 지수 변동 %.2f%% 미만 → VIX %.2f%% 무시", 
                             VIX_FILTER_THRESHOLD, delta)
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
        # 선물 시간 (미국 정규장 전)
        for key, sym in [("ΔES_FUT", ES_FUT), ("ΔNQ_FUT", NQ_FUT)]:
            delta, is_fresh = get_futures_data(sym)
            name = human_name(sym)
            
            if delta is None or not is_fresh:
                log.info("%s: 데이터 없음/오래됨", name)
                state[key] = None
                continue
            
            # 0.8% 미만은 무시
            if abs(delta) < 0.8:
                log.info("%s: %.2f%% (0.8%% 미만 → 알림 생략)", name, delta)
                state[key] = delta
                continue
            
            # 이전 값과 비교 (노이즈 억제)
            prev_val = state.get(key)
            if prev_val is not None:
                try:
                    if abs(delta - float(prev_val)) < 0.1:
                        log.info("%s: %.2f%% (변화 미미 → 알림 생략)", name, delta)
                        state[key] = delta
                        continue
                except:
                    pass
            
            note = "선물 시장 변동"
            post_alert(delta, "PRE", sym, note, kind="PRE")
            log.info("%s: %.2f%% [PRE 알림]", name, delta)
            state[key] = delta
    
    _save_state(state)
    log.info("===== 체크 완료 =====")

# ==================== 메인 루프 ====================
def run_loop():
    log.info("=== Sentinel 시장감시 시작 ===")
    log.info("간격: %d초, 신선도: %d초 이내", WATCH_INTERVAL, MAX_STALENESS_SEC)
    log.info("임계값 - 일반: 0.8%/1.5%/2.5%, VIX: 5%/7%/10%")
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
