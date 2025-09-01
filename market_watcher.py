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

# 한국 지수 심볼
KR = {
    "KOSPI": "^KS11",
    "K200_ETF1": "069500.KS",
    "K200_ETF2": "102110.KS",
    "KOSDAQ": "^KQ11"
}
# 미국 지수 심볼
US = {
    "SPX": "^GSPC", "NDX": "^IXIC", "VIX": "^VIX",
    "ES": "ES=F", "NQ": "NQ=F"
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

def _extract_intraday_change(quote: dict, max_age_sec: int = 1800) -> float | None:
    """정규장 데이터 우선 추출"""
    # 정규장 데이터 우선 사용
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

def _yf_change_intraday(symbol: str) -> float:
    if not _YF_READY: raise RuntimeError("yfinance not available")
    t = yf.Ticker(symbol)
    info = t.fast_info
    # regularMarketPrice 우선 시도
    last = getattr(info, "regularMarketPrice", None) or getattr(info, "last_price", None)
    prev = getattr(info, "regularMarketPreviousClose", None) or getattr(info, "previous_close", None)
    
    if last is not None and prev not in (None, 0):
        return (float(last) - float(prev)) / float(prev) * 100.0
    raise RuntimeError(f"no intraday for {symbol}")

# -------------------- KR 지표 (정규장 intraday only) --------------------
def get_kr_delta() -> tuple[float, str]:
    # 1) KOSPI
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                quotes = _yahoo_quote([KR["KOSPI"]])
                if quotes:
                    cp = _extract_intraday_change(quotes[0], KR_MAX_STALENESS)
                    if cp is not None:
                        return cp, "KOSPI"
            elif provider == "yfinance" and _YF_READY:
                return _yf_change_intraday(KR["KOSPI"]), "KOSPI"
        except Exception as e:
            log.debug("KOSPI %s 실패: %s", provider, e)

    # 2) K200 ETF 평균
    syms = [KR["K200_ETF1"], KR["K200_ETF2"]]
    try:
        quotes = _yahoo_quote(syms)
        vals = []
        for q in quotes:
            cp = _extract_intraday_change(q, KR_MAX_STALENESS)
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
                vals.append(_yf_change_intraday(s))
            except: 
                pass
        if vals:
            return sum(vals)/len(vals), "K200_ETF"

    # 3) KOSDAQ 보조
    try:
        quotes = _yahoo_quote([KR["KOSDAQ"]])
        if quotes:
            cp = _extract_intraday_change(quotes[0], KR_MAX_STALENESS)
            if cp is not None:
                return cp, "KOSDAQ"
    except Exception as e:
        log.debug("KOSDAQ 실패: %s", e)

    raise RuntimeError("KR intraday 실패")

# -------------------- US 지표 --------------------
def get_us_delta(symbol: str) -> float:
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                q = _yahoo_quote([symbol])
                if q:
                    cp = _extract_intraday_change(q[0], 3600)
                    if cp is not None:
                        return cp
            elif provider == "yfinance" and _YF_READY:
                return _yf_change_intraday(symbol)
        except Exception as e:
            log.debug("%s %s 실패: %s", symbol, provider, e)
    raise RuntimeError(f"{symbol} 수집 실패")

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
def post_alert(index_name: str, delta_pct: float, level: str | None, source: str, note: str):
    payload = {
        "index": index_name,
        "level": level or "CLEARED",
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": f"{note} [{source}]",
    }
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY: headers["x-sentinel-key"] = SENTINEL_KEY
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.ok:
            log.info("알림 전송: %s %s %.2f%% (%s)", index_name, level or "CLEARED", delta_pct or 0, note)
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
            log.info("KR(%s): %.2f%% (현재: %s, 이전: %s)", source, delta, level or "정상", prev or "정상")
            if level != prev:
                note = ("레벨 진입" if not prev else
                        "레벨 해제" if not level else
                        f"{prev}→{level}")
                idx = "ΔK200" if source.startswith("K200") else "ΔKOSPI"
                post_alert(idx, delta, level, source, note)
                state["KR_LEVEL"] = level
        except Exception as e:
            log.error("KR 수집 실패: %s", e)
    else:
        mo = is_us_market_open()
        log.info("US: %s", "개장" if mo else "마감(선물)")
        symbols = (
            [("ΔSPX", US["SPX"], "S&P500", False),
             ("ΔNASDAQ", US["NDX"], "NASDAQ", False),
             ("ΔVIX", US["VIX"], "VIX", True)]
            if mo else
            [("ΔES", US["ES"], "S&P500 선물", False),
             ("ΔNQ", US["NQ"], "NASDAQ 선물", False)]
        )
        for idx_name, sym, label, is_vix in symbols:
            try:
                delta = get_us_delta(sym)
                level = grade_level(delta, is_vix)
                prev  = state.get(idx_name)
                log.info("US %s: %.2f%% (현재: %s, 이전: %s)", label, delta, level or "정상", prev or "정상")
                if level != prev:
                    note = ("레벨 진입" if not prev else
                            "레벨 해제" if not level else
                            f"{prev}→{level}")
                    post_alert(idx_name, delta, level, sym, note)
                    state[idx_name] = level
            except Exception as e:
                log.warning("US %s 실패: %s", label, e)

    _save_state(state)
    log.info("===== 시장 체크 완료 =====")

def run_loop():
    log.info("=== Sentinel 시장 감시 시작 ===")
    log.info("간격: %d초", WATCH_INTERVAL)
    log.info("데이터 소스: %s", ", ".join(DATA_PROVIDERS))
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
