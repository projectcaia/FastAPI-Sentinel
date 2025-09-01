# market_watcher.py — FGPT Sentinel 시장감시 워커 (최종 안정화 버전)
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
KR_MAX_STALENESS_SEC = parse_int_env("KR_MAX_STALENESS_SEC", 600)  # 한국은 10분 이내로 더 엄격
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

# 심볼 정의 - 한국 ETF 우선 (더 안정적)
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
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "If-None-Match": "*",  # 캐시 방지 추가
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

# ==================== 한국 시장 전용 데이터 수집 (강화) ====================
def is_kr_market_open() -> bool:
    """한국 정규장 시간 체크"""
    now = _now_kst()
    if now.weekday() >= 5:  # 주말
        return False
    hhmm = now.hour * 100 + now.minute
    return 900 <= hhmm <= 1530

def get_kr_realtime_data(symbol: str) -> tuple[float | None, bool, str]:
    """한국 시장 실시간 데이터 수집 - 3중 검증"""
    
    # 정규장 시간 체크
    if not is_kr_market_open():
        log.debug("%s: 한국 정규장 시간 아님", symbol)
        return None, False, "market_closed"
    
    # 1차: Yahoo Chart API (분봉 우선 - 가장 신뢰성 높음)
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "interval": "2m",  # 2분봉
            "range": "1d",
            "includePrePost": "false",  # 정규장만
            "_nocache": str(int(time.time()))  # 캐시 방지
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        
        # 메타데이터에서 현재가
        current_price = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        timestamp = meta.get("regularMarketTime")
        volume = meta.get("regularMarketVolume", 0)
        
        # 거래량 체크 (거래량 0이면 장 마감)
        if volume and volume > 0:
            if current_price and prev_close and timestamp:
                age_sec = _utc_ts_now() - float(timestamp)
                is_fresh = age_sec <= KR_MAX_STALENESS_SEC
                
                if is_fresh:
                    change_pct = (float(current_price) - float(prev_close)) / float(prev_close) * 100.0
                    kst_time = _utc_to_kst(float(timestamp)).strftime("%H:%M:%S")
                    log.info("한국 Chart: %s = %.2f%% (가격: %.0f, 전일: %.0f, 시각: %s KST, 거래량: %d)", 
                            symbol, change_pct, float(current_price), float(prev_close), kst_time, volume)
                    return change_pct, True, f"chart_{kst_time}"
        
        # 캔들 데이터 체크
        quotes = chart.get("indicators", {}).get("quote", [{}])[0]
        closes = quotes.get("close", [])
        volumes = quotes.get("volume", [])
        timestamps = chart.get("timestamp", [])
        
        if closes and timestamps:
            # 마지막 유효 데이터 (거래량 있는 것만)
            valid_data = [(t, c, v) for t, c, v in zip(timestamps, closes, volumes or [0]*len(closes)) 
                         if c is not None and v and v > 0]
            if valid_data:
                last_ts, last_close, last_vol = valid_data[-1]
                age = _utc_ts_now() - last_ts
                
                if age <= KR_MAX_STALENESS_SEC and prev_close and prev_close != 0:
                    change_pct = (last_close - float(prev_close)) / float(prev_close) * 100.0
                    kst_time = _utc_to_kst(last_ts).strftime("%H:%M:%S")
                    log.info("한국 Chart 캔들: %s = %.2f%% (시각: %s, 거래량: %d)", 
                            symbol, change_pct, kst_time, last_vol)
                    return change_pct, True, f"candle_{kst_time}"
                    
    except Exception as e:
        log.debug("한국 Chart 실패(%s): %s", symbol, e)
    
    # 2차: Yahoo Quote API
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {
            "symbols": symbol,
            "crumb": str(int(time.time())),
            "formatted": "false",
            "_nocache": str(int(time.time()))
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            q = items[0]
            
            # marketState 체크
            market_state = q.get("marketState", "")
            if market_state != "REGULAR":
                log.debug("%s: 정규장 아님 (상태: %s)", symbol, market_state)
                # 정규장이 아니어도 최근 데이터면 사용
            
            price = q.get("regularMarketPrice")
            prev = q.get("regularMarketPreviousClose")
            timestamp = q.get("regularMarketTime")
            volume = q.get("regularMarketVolume", 0)
            
            if price and prev and timestamp and volume > 0:
                age_sec = _utc_ts_now() - float(timestamp)
                is_fresh = age_sec <= KR_MAX_STALENESS_SEC
                
                if is_fresh:
                    change_pct = (float(price) - float(prev)) / float(prev) * 100.0
                    kst_time = _utc_to_kst(float(timestamp)).strftime("%H:%M:%S")
                    log.info("한국 Quote: %s = %.2f%% (시각: %s KST, 상태: %s)", 
                            symbol, change_pct, kst_time, market_state)
                    return change_pct, True, f"quote_{kst_time}"
                    
    except Exception as e:
        log.debug("한국 Quote 실패(%s): %s", symbol, e)
    
    # 3차: yfinance
    if _YF_READY:
        try:
            ticker = yf.Ticker(symbol)
            
            # 오늘 분봉 데이터
            df = ticker.history(period="1d", interval="2m", prepost=False, actions=False)
            if df is not None and len(df) > 0:
                last_time = df.index[-1]
                last_close = float(df["Close"].iloc[-1])
                last_volume = float(df["Volume"].iloc[-1]) if "Volume" in df else 0
                
                if last_volume > 0:  # 거래량 체크
                    now = datetime.now(timezone.utc)
                    if hasattr(last_time, 'tz_localize'):
                        last_time_utc = last_time.tz_localize('UTC')
                    else:
                        last_time_utc = last_time
                    
                    age_sec = (now - last_time_utc).total_seconds()
                    
                    if age_sec <= KR_MAX_STALENESS_SEC:
                        info = ticker.info
                        prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
                        
                        if prev and prev != 0:
                            change_pct = (last_close - prev) / prev * 100.0
                            kst_time = _utc_to_kst(last_time_utc.timestamp()).strftime("%H:%M:%S")
                            log.info("한국 yfinance: %s = %.2f%% (시각: %s KST)", 
                                    symbol, change_pct, kst_time)
                            return change_pct, True, f"yf_{kst_time}"
                            
        except Exception as e:
            log.debug("한국 yfinance 실패(%s): %s", symbol, e)
    
    return None, False, "failed"

def get_kr_spot_data() -> tuple[float | None, str, str]:
    """한국 현물 데이터 수집 - 우선순위대로 시도"""
    
    for sym in KR_SPOT_PRIORITY:
        change_pct, is_fresh, source = get_kr_realtime_data(sym)
        if change_pct is not None and is_fresh:
            return change_pct, sym, source
        else:
            log.debug("%s: 실시간 데이터 수집 실패 (%s)", human_name(sym), source)
    
    log.warning("한국 시장: 모든 심볼 데이터 수집 실패")
    return None, "", "none"

# ==================== 미국 시장 데이터 수집 (강화) ====================
def is_us_market_open() -> bool:
    """미국 정규장 시간 체크"""
    now = _now_kst()
    if now.weekday() >= 5:
        return False
    
    hour = now.hour
    minute = now.minute
    month = now.month
    
    # DST 정확한 계산 (3월 둘째 일요일 ~ 11월 첫째 일요일)
    is_dst = (month > 3 and month < 11) or \
             (month == 3 and now.day >= 8) or \
             (month == 11 and now.day <= 7)
    
    if is_dst:
        # 22:30 ~ 05:00 KST
        if hour == 22 and minute >= 30:
            return True
        elif hour >= 23 or hour < 5:
            return True
    else:
        # 23:30 ~ 06:00 KST
        if hour == 23 and minute >= 30:
            return True
        elif hour >= 0 and hour < 6:
            return True
    
    return False

def get_us_spot_data(symbol: str) -> tuple[float | None, bool]:
    """미국 현물 데이터 수집"""
    
    # 1차: Chart API
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "interval": "5m",
            "range": "1d",
            "includePrePost": "false" if is_us_market_open() else "true",
            "_nocache": str(int(time.time()))
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        
        current_price = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose")
        timestamp = meta.get("regularMarketTime")
        
        if current_price and prev_close and timestamp:
            age_sec = _utc_ts_now() - float(timestamp)
            is_fresh = age_sec <= MAX_STALENESS_SEC
            
            if is_fresh and prev_close != 0:
                change_pct = (float(current_price) - float(prev_close)) / float(prev_close) * 100.0
                log.debug("미국 Chart %s: %.2f%% (age=%ds)", symbol, change_pct, age_sec)
                return change_pct, True
                
    except Exception as e:
        log.debug("미국 Chart 실패(%s): %s", symbol, e)
    
    # 2차: Quote API
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {
            "symbols": symbol,
            "crumb": str(int(time.time())),
            "_nocache": str(int(time.time()))
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            q = items[0]
            
            # marketState 확인
            market_state = q.get("marketState", "")
            
            # 정규장 시간이면 regularMarket 데이터만
            if market_state == "REGULAR" or is_us_market_open():
                price = q.get("regularMarketPrice")
                prev = q.get("regularMarketPreviousClose")
                timestamp = q.get("regularMarketTime")
            else:
                # 시간외는 post/pre 데이터도 사용
                price = q.get("postMarketPrice") or q.get("preMarketPrice") or q.get("regularMarketPrice")
                timestamp = q.get("postMarketTime") or q.get("preMarketTime") or q.get("regularMarketTime")
                prev = q.get("regularMarketPreviousClose")
            
            if price and prev and timestamp:
                age_sec = _utc_ts_now() - float(timestamp)
                if age_sec <= MAX_STALENESS_SEC and prev != 0:
                    change_pct = (float(price) - float(prev)) / float(prev) * 100.0
                    log.debug("미국 Quote %s: %.2f%% (상태: %s)", symbol, change_pct, market_state)
                    return change_pct, True
                    
    except Exception as e:
        log.debug("미국 Quote 실패(%s): %s", symbol, e)
    
    return None, False

# ==================== 선물 데이터 수집 (기존 유지) ====================
def get_futures_data(symbol: str) -> tuple[float | None, bool, str]:
    """선물 데이터 수집 - 여러 방법 시도"""
    
    # 1차: Yahoo Chart API
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "interval": "2m",
            "range": "1d",
            "includePrePost": "true",
            "events": "div,split",
            "_nocache": str(int(time.time()))
        }
        
        headers = {
            **H_COMMON,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        
        current_price = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        
        if current_price and prev_close and prev_close != 0:
            change_pct = (float(current_price) - float(prev_close)) / float(prev_close) * 100.0
            log.debug("Chart API 선물 %s: %.2f%%", symbol, change_pct)
            return change_pct, True, "chart_meta"
            
    except Exception as e:
        log.debug("Chart API 선물 실패(%s): %s", symbol, e)
    
    # 2차: Quote API
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {
            "symbols": symbol,
            "fields": "symbol,regularMarketPrice,regularMarketChangePercent,regularMarketPreviousClose,regularMarketTime",
            "crumb": str(int(time.time())),
            "_nocache": str(int(time.time()))
        }
        
        r = _http_get(url, params=params)
        data = r.json()
        
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            q = items[0]
            price = q.get("regularMarketPrice")
            prev = q.get("regularMarketPreviousClose")
            
            if price and prev and prev != 0:
                change_pct = (float(price) - float(prev)) / float(prev) * 100.0
                log.debug("Quote API 선물 %s: %.2f%%", symbol, change_pct)
                return change_pct, True, "quote"
                
    except Exception as e:
        log.debug("Quote API 선물 실패(%s): %s", symbol, e)
    
    return None, False, "failed"

# ==================== 시장 시간 판정 ====================
def is_market_holiday() -> bool:
    """주말/휴일 체크"""
    now = _now_kst()
    if now.weekday() >= 5:
        return True
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
    
    # 미국 정규장 체크
    if is_us_market_open():
        return "US"
    
    # 선물 시간 (한국 장 마감 후 ~ 미국 장 시작 전)
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
    
    log.info("===== 시장 체크 [%s] %s =====", sess, _now_kst().strftime("%Y-%m-%d %H:%M:%S KST"))
    state["last_checked_at"] = _now_kst_iso()
    
    if sess == "CLOSED":
        log.info("시장 휴장 - 감시 스킵")
        _save_state(state)
        return
    
    if sess == "KR":
        # ★★★ 한국 정규장 - 가장 중요 ★★★
        log.info("한국 정규장 데이터 수집 시작...")
        delta, tag, source = get_kr_spot_data()
        
        if delta is None:
            log.warning("한국 시장 데이터 수집 실패 - 정규장 시간이 아니거나 데이터 없음")
            return
        
        lvl = grade_level(delta)
        prev = state.get("ΔK200")
        name = human_name(tag)
        
        log.info("★ %s: %.2f%% [%s → %s] (소스: %s)", name, delta, prev or "없음", lvl or "정상", source)
        
        if lvl != prev:
            note = ("레벨 진입" if (not prev and lvl) else
                   "레벨 해제" if (prev and not lvl) else
                   f"{prev} → {lvl}")
            note += f" (소스: {source})"
            post_alert(delta, lvl, tag, note)
            state["ΔK200"] = lvl
    
    elif sess == "US":
        # 미국 정규장
        log.info("미국 정규장 데이터 수집...")
        
        # S&P, NASDAQ 먼저 수집 (VIX 필터용)
        spx_delta, spx_fresh = get_us_spot_data("^GSPC")
        ndx_delta, ndx_fresh = get_us_spot_data("^IXIC")
        
        for sym in US_SPOT:
            delta, is_fresh = get_us_spot_data(sym)
            if delta is None or not is_fresh:
                log.warning("%s 데이터 수집 실패", human_name(sym))
                continue
            
            is_vix = (sym == "^VIX")
            
            # VIX 필터
            if is_vix and spx_delta is not None and ndx_delta is not None:
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
        # 선물 시간
        log.info("선물 시장 데이터 수집...")
        
        for key, sym in [("ΔES_FUT", "ES=F"), ("ΔNQ_FUT", "NQ=F")]:
            delta, is_fresh, source = get_futures_data(sym)
            name = human_name(sym)
            
            if delta is None or not is_fresh:
                log.info("%s: 데이터 없음/오래됨", name)
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
            
            note = f"선물 시장 변동 (소스: {source})"
            post_alert(delta, "PRE", sym, note, kind="PRE")
            log.info("%s: %.2f%% [PRE 알림]", name, delta)
            state[key] = delta
    
    _save_state(state)
    log.info("===== 체크 완료 =====")

# ==================== 메인 루프 ====================
def run_loop():
    log.info("=== Sentinel 시장감시 시작 (최종 안정화 버전) ===")
    log.info("간격: %d초", WATCH_INTERVAL)
    log.info("신선도: 일반 %d초, 한국 %d초 이내", MAX_STALENESS_SEC, KR_MAX_STALENESS_SEC)
    log.info("임계값 - 일반: 0.8%/1.5%/2.5%, VIX: 5%/7%/10%")
    log.info("VIX 필터: 지수 변동 %.1f%% 미만시 무시", VIX_FILTER_THRESHOLD)
    log.info("한국 우선순위: %s", " → ".join([human_name(s) for s in KR_SPOT_PRIORITY]))
    
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
