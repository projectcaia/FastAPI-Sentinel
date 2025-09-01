# market_watcher.py — Sentinel 시장감시 워커 (정규장 intraday 전용 버전)

import os, time, json, logging, requests, random
from datetime import datetime, timezone, timedelta

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
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "").strip()

def _pint(key, default):
    import re
    m = re.search(r"\d+", os.getenv(key, str(default)))
    return int(m.group()) if m else default

WATCH_INTERVAL = _pint("WATCH_INTERVAL_SEC", 1800)
STATE_PATH     = os.getenv("WATCHER_STATE_PATH", "./market_state.json")
US_BASELINE_PATH = os.getenv("US_BASELINE_PATH", "./us_baseline.json")

# -------------------- 데이터 소스 --------------------
YF_ENABLED = os.getenv("YF_ENABLED", "true").lower() in ("1","true","yes")
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS","yahoo,yfinance").split(",") if s.strip()]

_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance 로드 실패: %s", e)

# 한국 지수 심볼 및 이름
KR = {
    "KOSPI": "^KS11",
    "K200_ETF1": "069500.KS",
    "K200_ETF2": "102110.KS",
    "KOSDAQ": "^KQ11"
}

KR_NAMES = {
    "KOSPI": "한국 시장: 코스피",
    "K200_ETF": "한국 시장: KODEX 200",
    "KOSDAQ": "한국 시장: 코스닥"
}

# 미국 지수 심볼 및 이름
US = {
    "SPX": "^GSPC", 
    "NDX": "^IXIC", 
    "VIX": "^VIX",
    "ES": "ES=F", 
    "NQ": "NQ=F"
}

US_NAMES = {
    "SPX": "미국 S&P500",
    "NDX": "미국 NASDAQ",
    "VIX": "미국 VIX: 변동성지수",
    "ES": "미국 S&P500 선물",
    "NQ": "미국 NASDAQ 선물"
}

# intraday 데이터 신선도 (초)
KR_MAX_STALENESS = _pint("KR_MAX_STALENESS_SEC", 1800)  # 30분

# -------------------- 시간/세션 --------------------
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def current_session() -> str:
    now = _now_kst()
    if now.weekday() >= 5:  # 주말
        return "US"
    hhmm = now.hour * 100 + now.minute
    return "KR" if 830 <= hhmm <= 1600 else "US"

def is_us_market_open() -> bool:
    now = _now_kst()
    if now.weekday() >= 5:  # 주말
        return False
    h, m = now.hour, now.minute
    is_dst = 3 <= now.month <= 11
    if is_dst:  # 22:30~05:00
        if h == 22 and m >= 30: return True
        return 23 <= h or h < 5
    else:      # 23:30~06:00
        if h == 23 and m >= 30: return True
        return 0 <= h < 6

# -------------------- 상태/HTTP --------------------
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

def _save_baseline(baseline: dict):
    try:
        with open(US_BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("베이스라인 저장 실패: %s", e)

def _load_baseline() -> dict:
    if not os.path.exists(US_BASELINE_PATH):
        return {}
    try:
        with open(US_BASELINE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _http_get(url: str, params=None, timeout=12, max_retry=3):
    headers = {
        "User-Agent":"Mozilla/5.0",
        "Accept":"application/json, text/plain, */*",
        "Referer":"https://finance.yahoo.com/"
    }
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception:
            if i < max_retry - 1:
                time.sleep(1+i)
                continue
            raise
    raise RuntimeError("HTTP 요청 실패")

# -------------------- Yahoo / yfinance --------------------
def _yahoo_quote(symbols):
    sym = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": sym}, timeout=15)
    return r.json().get("quoteResponse", {}).get("result", [])

def _extract_kr_intraday_change(quote: dict, max_age_sec: int = 1800) -> float | None:
    """한국 시장: 정규장 데이터 우선 추출 (전일 종가 대비)"""
    price = quote.get("regularMarketPrice") or quote.get("price")
    prev = quote.get("regularMarketPreviousClose") or quote.get("previousClose")
    rmt = quote.get("regularMarketTime") or quote.get("marketTime")
    
    if price is None or prev in (None, 0):
        return None
    
    # 신선도 체크
    try:
        if isinstance(rmt, (int,float)):
            age = _now_kst().timestamp() - float(rmt)
            if not (0 <= age <= max_age_sec):
                return None
    except Exception:
        return None
    
    return (float(price) - float(prev)) / float(prev) * 100.0

def _extract_us_current_price(quote: dict) -> float | None:
    """미국 시장: 현재가 추출"""
    # 정규장 시간이면 regularMarketPrice, 아니면 price (시간외 거래가)
    price = quote.get("regularMarketPrice") or quote.get("price")
    return float(price) if price is not None else None

def _yf_kr_change_intraday(symbol: str) -> float:
    """한국: 전일 종가 대비 변화율"""
    if not _YF_READY: raise RuntimeError("yfinance not available")
    t = yf.Ticker(symbol)
    info = t.fast_info
    last = getattr(info, "regularMarketPrice", None) or getattr(info, "last_price", None)
    prev = getattr(info, "regularMarketPreviousClose", None) or getattr(info, "previous_close", None)
    
    if last is not None and prev not in (None, 0):
        return (float(last) - float(prev)) / float(prev) * 100.0
    raise RuntimeError(f"no intraday for {symbol}")

def _yf_us_current_price(symbol: str) -> float:
    """미국: 현재가"""
    if not _YF_READY: raise RuntimeError("yfinance not available")
    t = yf.Ticker(symbol)
    info = t.fast_info
    price = getattr(info, "last_price", None)
    if price is not None:
        return float(price)
    raise RuntimeError(f"no price for {symbol}")

# -------------------- KR 지표 (정규장 intraday only) --------------------
def get_kr_delta() -> tuple[float, str]:
    # 1) KOSPI
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                quotes = _yahoo_quote([KR["KOSPI"]])
                if quotes:
                    cp = _extract_kr_intraday_change(quotes[0], KR_MAX_STALENESS)
                    if cp is not None:
                        return cp, "KOSPI"
            elif provider == "yfinance" and _YF_READY:
                return _yf_kr_change_intraday(KR["KOSPI"]), "KOSPI"
        except Exception as e:
            log.debug("KOSPI %s 실패: %s", provider, e)

    # 2) K200 ETF 평균
    syms = [KR["K200_ETF1"], KR["K200_ETF2"]]
    try:
        quotes = _yahoo_quote(syms)
        vals = []
        for q in quotes:
            cp = _extract_kr_intraday_change(q, KR_MAX_STALENESS)
            if cp is not None: 
                vals.append(cp)
        if vals:
            return sum(vals)/len(vals), "K200_ETF"
    except Exception as e:
        log.debug("K200_ETF yahoo 실패: %s", e)
    
    if _YF_READY:
        vals = []
        for s in syms:
            try: 
                vals.append(_yf_kr_change_intraday(s))
            except: 
                pass
        if vals:
            return sum(vals)/len(vals), "K200_ETF"

    # 3) KOSDAQ 보조
    try:
        quotes = _yahoo_quote([KR["KOSDAQ"]])
        if quotes:
            cp = _extract_kr_intraday_change(quotes[0], KR_MAX_STALENESS)
            if cp is not None:
                return cp, "KOSDAQ"
    except Exception as e:
        log.debug("KOSDAQ 실패: %s", e)

    raise RuntimeError("KR intraday 실패")

# -------------------- US 지표 (현재가 기준) --------------------
def get_us_current_price(symbol: str) -> float:
    """미국 지수 현재가 가져오기"""
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                q = _yahoo_quote([symbol])
                if q:
                    price = _extract_us_current_price(q[0])
                    if price is not None:
                        return price
            elif provider == "yfinance" and _YF_READY:
                return _yf_us_current_price(symbol)
        except Exception as e:
            log.debug("%s %s 실패: %s", symbol, provider, e)
    raise RuntimeError(f"{symbol} 가격 수집 실패")

def update_us_baseline(force_update: bool = False):
    """미국 시장 개장시 베이스라인 업데이트"""
    baseline = _load_baseline()
    now = _now_kst()
    last_update = baseline.get("last_update", "")
    
    # 오늘 이미 업데이트했으면 스킵 (force_update가 아닌 경우)
    if not force_update and last_update.startswith(now.strftime("%Y-%m-%d")):
        return baseline
    
    # 미국 시장 개장 직후에만 업데이트 (KST 22:30~23:30 or 23:30~00:30)
    h, m = now.hour, now.minute
    is_dst = 3 <= now.month <= 11
    should_update = False
    
    if is_dst and h == 22 and m >= 30:
        should_update = True
    elif is_dst and h == 23 and m < 30:
        should_update = True
    elif not is_dst and h == 23 and m >= 30:
        should_update = True
    elif not is_dst and h == 0 and m < 30:
        should_update = True
    
    if should_update or force_update:
        try:
            # 현물 지수 베이스라인 설정
            baseline["SPX"] = get_us_current_price(US["SPX"])
            baseline["NDX"] = get_us_current_price(US["NDX"])
            baseline["VIX"] = get_us_current_price(US["VIX"])
            baseline["last_update"] = _now_kst_iso()
            _save_baseline(baseline)
            log.info("미국 베이스라인 업데이트: SPX=%.2f, NDX=%.2f, VIX=%.2f", 
                     baseline["SPX"], baseline["NDX"], baseline["VIX"])
        except Exception as e:
            log.error("베이스라인 업데이트 실패: %s", e)
    
    return baseline

def get_us_delta_from_baseline(symbol_key: str, current_price: float) -> float:
    """베이스라인 대비 변화율 계산"""
    baseline = _load_baseline()
    
    # 선물의 경우 매핑
    mapping = {
        "ES": "SPX",  # ES 선물 -> SPX 베이스라인
        "NQ": "NDX"   # NQ 선물 -> NDX 베이스라인
    }
    
    baseline_key = mapping.get(symbol_key, symbol_key)
    base_price = baseline.get(baseline_key)
    
    if base_price and base_price != 0:
        return (current_price - base_price) / base_price * 100.0
    else:
        # 베이스라인이 없으면 0 반환 (첫 실행시)
        log.warning("%s 베이스라인 없음", baseline_key)
        return 0.0

# -------------------- 레벨 --------------------
def grade_level(delta_pct: float, is_vix: bool = False) -> str | None:
    a = abs(delta_pct)
    if is_vix:
        if a >= 10.0: return "LV3"
        if a >= 7.0:  return "LV2"
        if a >= 5.0:  return "LV1"
    else:
        if a >= 2.5: return "LV3"
        if a >= 1.5: return "LV2"
        if a >= 0.8: return "LV1"
    return None

# -------------------- 알림 --------------------
def post_alert(index_name: str, delta_pct: float, level: str | None, display_name: str, note: str):
    payload = {
        "index": display_name,
        "level": level or "CLEARED",
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": note,
    }
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY: headers["x-sentinel-key"] = SENTINEL_KEY
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.ok:
            log.info("알림 전송: %s %s %.2f%% (%s)", display_name, level or "CLEARED", delta_pct or 0, note)
        else:
            log.error("알림 실패: %s %s", r.status_code, r.text)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# -------------------- 메인 --------------------
def check_and_alert():
    state = _load_state()
    sess = current_session()
    log.info("===== 시장 체크 시작 (%s 세션) =====", sess)

    if sess == "KR":
        try:
            delta, source = get_kr_delta()
            level = grade_level(delta)
            prev = state.get("KR_LEVEL")
            display_name = KR_NAMES.get(source, source)
            log.info("KR(%s): %.2f%% (현재: %s, 이전: %s)", source, delta, level or "정상", prev or "정상")
            if level != prev:
                note = ("레벨 진입" if not prev else
                        "레벨 해제" if not level else
                        f"{prev}→{level}")
                post_alert(source, delta, level, display_name, note)
                state["KR_LEVEL"] = level
        except Exception as e:
            log.error("KR 수집 실패: %s", e)
    else:
        mo = is_us_market_open()
        log.info("US: %s", "개장" if mo else "마감(선물)")
        
        # 미국 시장 개장시 베이스라인 업데이트
        if mo:
            update_us_baseline()
        
        # 심볼 선택
        if mo:
            symbols = [
                ("SPX", US["SPX"], False),
                ("NDX", US["NDX"], False),
                ("VIX", US["VIX"], True)
            ]
        else:
            symbols = [
                ("ES", US["ES"], False),
                ("NQ", US["NQ"], False)
            ]
        
        for key, sym, is_vix in symbols:
            try:
                # 현재가 가져오기
                current_price = get_us_current_price(sym)
                # 베이스라인 대비 변화율 계산
                delta = get_us_delta_from_baseline(key, current_price)
                level = grade_level(delta, is_vix)
                prev = state.get(key)
                display_name = US_NAMES.get(key, key)
                log.info("US %s: 현재가=%.2f, 변화율=%.2f%% (현재: %s, 이전: %s)", 
                         key, current_price, delta, level or "정상", prev or "정상")
                if level != prev:
                    note = ("레벨 진입" if not prev else
                            "레벨 해제" if not level else
                            f"{prev}→{level}")
                    post_alert(key, delta, level, display_name, note)
                    state[key] = level
            except Exception as e:
                log.warning("US %s 실패: %s", key, e)

    _save_state(state)
    log.info("===== 시장 체크 완료 =====")

def run_loop():
    log.info("=== Sentinel 시장 감시 시작 ===")
    log.info("간격: %d초", WATCH_INTERVAL)
    log.info("데이터 소스: %s", ", ".join(DATA_PROVIDERS))
    
    # 초기 베이스라인 설정 (미국 시장)
    try:
        update_us_baseline(force_update=True)
    except Exception as e:
        log.warning("초기 베이스라인 설정 실패: %s", e)
    
    try:
        check_and_alert()
    except Exception as e:
        log.error("초기 체크 실패: %s", e)
    
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            check_and_alert()
        except Exception as e:
            log.error("주기 체크 오류: %s", e)

if __name__ == "__main__":
    run_loop()
