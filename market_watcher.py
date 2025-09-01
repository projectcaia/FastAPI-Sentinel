# market_watcher.py — FGPT Sentinel 시장감시 워커 (정규장 실시간 반영 버전)
# 주 요약
# - KR 정규장: Yahoo regularMarketPrice / yfinance fast_info.last_price 우선 → 장중 반영
# - US 세션: 개장 시 현물(SPX/NDX/VIX), 마감 시 선물(ES/NQ) 감시
# - 실패 시 ETF/대체 심볼 평균으로 폴백
# - 레벨(LV1/2/3) 변화시에만 알람 전송 (중복/노이즈 억제)
# - WATCH_INTERVAL(초) 환경변수로 주기 제어 (기본 1800=30분)

import os, time, json, logging, requests, math, random
from datetime import datetime, timezone, timedelta

# (선택) 외부 유틸 — 정규장 판정에 활용 (없어도 동작)
try:
    from app.utils import is_market_open as _ext_is_market_open
except Exception:
    _ext_is_market_open = None

# -------------------- 설정/로그 --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
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
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "").strip()

def _parse_int_env(key: str, default: int) -> int:
    value = os.getenv(key, str(default))
    import re
    m = re.search(r"\d+", value)
    return int(m.group()) if m else default

def _parse_float_env(key: str, default: float) -> float:
    value = os.getenv(key, str(default))
    import re
    m = re.search(r"[\d.]+", value)
    try:
        return float(m.group()) if m else default
    except Exception:
        return default

WATCH_INTERVAL = _parse_int_env("WATCH_INTERVAL_SEC", 1800)  # 기본 30분
STATE_PATH     = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# 멀티소스 설정
YF_ENABLED     = os.getenv("YF_ENABLED", "false").lower() in ("1", "true", "yes")
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS", "yahoo,yfinance,alphavantage").split(",") if s.strip()]
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()

# yfinance 초기화 (한 번만)
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance 로드 실패: %s", e)

# 한국 지수/ETF 심볼
KR_SYMBOLS = {
    "KOSPI": "^KS11",           # KOSPI 본지수
    "KOSDAQ": "^KQ11",          # KOSDAQ (보조)
    "K200_ETF1": "069500.KS",   # KODEX 200 ETF
    "K200_ETF2": "102110.KS",   # TIGER 200 ETF
}

# 미국 지수/선물 심볼
US_SYMBOLS = {
    "SPX": "^GSPC",
    "NDX": "^IXIC",
    "VIX": "^VIX",
    "SPX_FUT": "ES=F",
    "NDX_FUT": "NQ=F",
}

# Alpha Vantage 프록시 맵
AV_PROXY_MAP = {
    "^GSPC": "SPY",
    "^IXIC": "QQQ",
    "^VIX": "VIXY",
    "^KS11": "EWY",  # iShares MSCI South Korea ETF (한국 대체)
}

# 공통 헤더
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Cache-Control": "no-cache",
}

# -------------------- 시간/세션 --------------------
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def current_session() -> str:
    """KST 기준 세션 구분: 한국 정규장(08:30~16:00) or 미국(그 외/주말)"""
    now = _now_kst()
    if now.weekday() >= 5:  # 토/일
        return "US"
    hhmm = now.hour * 100 + now.minute
    return "KR" if 830 <= hhmm <= 1600 else "US"

def is_kr_market_open() -> bool:
    """외부 유틸 우선 → Fallback: 시간대 기반"""
    if _ext_is_market_open:
        try:
            return bool(_ext_is_market_open(market="KR"))
        except Exception:
            pass
    # 기본: 평일 09:00~15:30 (프리/애프터는 제외), 여기서는 08:30~16:00을 완화 허용
    sess = current_session()
    return (sess == "KR")

def is_us_market_open() -> bool:
    """미국장 개장 판정(간단 버전: DST 고려, KST 기준)"""
    now = _now_kst()
    hour, minute = now.hour, now.minute
    # 대략적 DST 범위: 3~11월
    is_dst = 3 <= now.month <= 11
    if is_dst:  # 서머타임: 22:30~05:00 (KST)
        if hour == 22 and minute >= 30:
            return True
        return 23 <= hour or hour < 5
    else:      # 표준시: 23:30~06:00 (KST)
        if hour == 23 and minute >= 30:
            return True
        return 0 <= hour < 6

# -------------------- 상태 관리 --------------------
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

# -------------------- HTTP 유틸 --------------------
def _http_get(url: str, params=None, timeout=12, max_retry=3):
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 429, 502, 503):
                time.sleep(2 ** i + random.random())
                continue
            raise
        except Exception:
            if i < max_retry - 1:
                time.sleep(1.0 + i + random.random())
                continue
            raise
    raise RuntimeError("HTTP 요청 실패")

# -------------------- Yahoo Finance --------------------
def _yahoo_quote(symbols):
    symbols_str = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": symbols_str}, timeout=15)
    data = r.json()
    return data.get("quoteResponse", {}).get("result", [])

def _extract_change_percent(quote: dict) -> float | None:
    """
    장중 실시간 우선:
    - regularMarketChangePercent → 즉시 반환
    - regularMarketPrice / regularMarketPreviousClose로 계산
    - 실패 시 None
    """
    v = quote.get("regularMarketChangePercent")
    if v is not None:
        try:
            return float(v)
        except Exception:
            pass

    price = quote.get("regularMarketPrice")
    prev  = quote.get("regularMarketPreviousClose") or quote.get("previousClose")
    try:
        if price is not None and prev not in (None, 0):
            return (float(price) - float(prev)) / float(prev) * 100.0
    except Exception:
        pass
    return None

# -------------------- yfinance --------------------
def _yf_change_percent(symbol: str) -> float:
    """
    장중 실시간 우선:
    - fast_info.last_price / previous_close
    - 실패 시 2일치 일봉 종가 비교(백업)
    """
    if not _YF_READY:
        raise RuntimeError("yfinance not available")

    ticker = yf.Ticker(symbol)

    # 1) fast_info 기반 (장중 갱신)
    try:
        info = ticker.fast_info
        last = getattr(info, "last_price", None)
        prev = getattr(info, "previous_close", None)
        if last is not None and prev not in (None, 0):
            return (float(last) - float(prev)) / float(prev) * 100.0
    except Exception:
        pass

    # 2) 2일치 종가 비교 (백업)
    hist = ticker.history(period="2d", interval="1d")
    if len(hist) >= 2:
        prev = hist["Close"].iloc[-2]
        last = hist["Close"].iloc[-1]
        if prev != 0:
            return (float(last) - float(prev)) / float(prev) * 100.0

    raise RuntimeError(f"yfinance data insufficient for {symbol}")

# -------------------- Alpha Vantage --------------------
def _alphavantage_change_percent(symbol: str) -> float:
    if not ALPHAVANTAGE_API_KEY:
        raise RuntimeError("ALPHAVANTAGE_API_KEY not set")
    proxy = AV_PROXY_MAP.get(symbol, symbol)
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": proxy, "apikey": ALPHAVANTAGE_API_KEY}
    r = _http_get(url, params=params, timeout=15)
    data = r.json()
    if "Note" in data or "Information" in data:
        raise RuntimeError(f"AV rate limit or info: {data}")
    quote = data.get("Global Quote", {})
    cp_str = quote.get("10. change percent", "")
    if cp_str:
        try:
            return float(cp_str.strip().rstrip("%"))
        except Exception:
            pass
    # 일부 종목은 percent 대신 price/prevPrice로 계산
    try:
        price = float(quote.get("05. price"))
        prev  = float(quote.get("08. previous close"))
        if prev:
            return (price - prev) / prev * 100.0
    except Exception:
        pass
    raise RuntimeError(f"AV insufficient for {proxy}")

# -------------------- 한국 지수 수집 (정규장 실시간 우선) --------------------
def get_kr_delta() -> tuple[float, str]:
    """
    한국 KOSPI 변동률 수집: 정규장 실시간 우선 → 실패 시 ETF 평균 → KOSDAQ 보조
    반환: (변동률[%], 소스라벨)
    """
    # 1) KOSPI 본지수(실시간 우선)
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                quotes = _yahoo_quote([KR_SYMBOLS["KOSPI"]])
                if quotes:
                    cp = _extract_change_percent(quotes[0])
                    if cp is not None:
                        return float(cp), "KOSPI"
            elif provider == "yfinance" and _YF_READY:
                cp = _yf_change_percent(KR_SYMBOLS["KOSPI"])
                return float(cp), "KOSPI"
            elif provider == "alphavantage" and ALPHAVANTAGE_API_KEY:
                cp = _alphavantage_change_percent(KR_SYMBOLS["KOSPI"])
                return float(cp), "KOSPI"
        except Exception as e:
            log.debug("KOSPI %s 실패: %s", provider, e)

    # 2) K200 ETF 평균 (069500.KS, 102110.KS)
    etf_syms = [KR_SYMBOLS["K200_ETF1"], KR_SYMBOLS["K200_ETF2"]]
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                quotes = _yahoo_quote(etf_syms)
                changes = []
                for q in quotes:
                    cp = _extract_change_percent(q)
                    if cp is not None:
                        changes.append(cp)
                if changes:
                    return float(sum(changes) / len(changes)), "K200_ETF"
            elif provider == "yfinance" and _YF_READY:
                changes = []
                for s in etf_syms:
                    try:
                        changes.append(_yf_change_percent(s))
                    except Exception:
                        pass
                if changes:
                    return float(sum(changes) / len(changes)), "K200_ETF"
        except Exception as e:
            log.debug("K200 ETF %s 실패: %s", provider, e)

    # 3) KOSDAQ 보조
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                quotes = _yahoo_quote([KR_SYMBOLS["KOSDAQ"]])
                if quotes:
                    cp = _extract_change_percent(quotes[0])
                    if cp is not None:
                        return float(cp), "KOSDAQ"
        except Exception as e:
            log.debug("KOSDAQ %s 실패: %s", provider, e)

    raise RuntimeError("한국 지수 수집 실패")

# -------------------- 미국 지수 수집 --------------------
def get_us_delta(symbol: str) -> float:
    """미국 지수 변동률 수집: 야후→yfinance→AV 순서"""
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                quotes = _yahoo_quote([symbol])
                if quotes:
                    cp = _extract_change_percent(quotes[0])
                    if cp is not None:
                        return float(cp)
            elif provider == "yfinance" and _YF_READY:
                return _yf_change_percent(symbol)
            elif provider == "alphavantage" and ALPHAVANTAGE_API_KEY:
                return _alphavantage_change_percent(symbol)
        except Exception as e:
            log.debug("%s %s 실패: %s", symbol, provider, e)
    raise RuntimeError(f"{symbol} 수집 실패")

# -------------------- 레벨 판정 --------------------
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

# -------------------- 알림 전송 --------------------
def post_alert(index_name: str, delta_pct: float, level: str | None, source: str, note: str):
    display_level = level if level else "CLEARED"
    payload = {
        "index": index_name,
        "level": display_level,
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": f"{note} [{source}]",
    }
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.ok:
            log.info("알림 전송: %s %s %.2f%% (%s)", index_name, display_level, delta_pct or 0, note)
        else:
            log.error("알림 실패: %s %s", r.status_code, r.text)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# -------------------- 메인 체크 --------------------
def check_and_alert():
    state = _load_state()
    sess = current_session()
    log.info("===== 시장 체크 시작 (%s 세션) =====", sess)

    if sess == "KR":
        # 한국 시장 — 정규장 실시간 우선
        try:
            market_open = is_kr_market_open()
            delta, source = get_kr_delta()
            level = grade_level(delta, is_vix=False)
            prev_level = state.get("KR_LEVEL")

            log.info("한국 시장(%s): %.2f%% (현재: %s, 이전: %s, 개장: %s)",
                     source, delta, level or "정상", prev_level or "정상", "Y" if market_open else "N")

            # 레벨 변화시에만 푸시
            if level != prev_level:
                if not prev_level:
                    note = "한국 시장: 레벨 진입"
                elif not level:
                    note = "한국 시장: 레벨 해제"
                else:
                    note = f"한국 시장: {prev_level}→{level}"
                post_alert("ΔKOSPI", delta, level, source, note)
                state["KR_LEVEL"] = level

        except Exception as e:
            log.error("한국 시장 감시 실패: %s", e)

    else:
        # 미국 시장 — 개장 시 현물, 마감 시 선물
        market_open = is_us_market_open()
        log.info("미국 시장: %s", "개장" if market_open else "마감(선물)")

        if market_open:
            symbols = [
                ("ΔSPX", US_SYMBOLS["SPX"], "S&P500", False),
                ("ΔNASDAQ", US_SYMBOLS["NDX"], "NASDAQ", False),
                ("ΔVIX", US_SYMBOLS["VIX"], "VIX", True),
            ]
        else:
            symbols = [
                ("ΔES", US_SYMBOLS["SPX_FUT"], "S&P500 선물", False),
                ("ΔNQ", US_SYMBOLS["NDX_FUT"], "NASDAQ 선물", False),
            ]

        for idx_name, symbol, label, is_vix in symbols:
            try:
                delta = get_us_delta(symbol)
                level = grade_level(delta, is_vix=is_vix)
                prev_level = state.get(idx_name)

                log.info("미국 %s: %.2f%% (현재: %s, 이전: %s)",
                         label, delta, level or "정상", prev_level or "정상")

                if level != prev_level:
                    if not prev_level:
                        note = f"미국 {label}: 레벨 진입"
                    elif not level:
                        note = f"미국 {label}: 레벨 해제"
                    else:
                        note = f"미국 {label}: {prev_level}→{level}"

                    post_alert(idx_name, delta, level, symbol, note)
                    state[idx_name] = level

            except Exception as e:
                log.warning("미국 %s 감시 실패: %s", label, e)

    _save_state(state)
    log.info("===== 시장 체크 완료 =====")

# -------------------- 루프 --------------------
def run_loop():
    log.info("=== Sentinel 시장 감시 시작 ===")
    log.info("간격: %d초", WATCH_INTERVAL)
    log.info("한국: KOSPI(^KS11) 실시간 우선, ETF 보조")
    log.info("미국: 현물(개장) / 선물(마감)")
    log.info("임계값: LV1=±0.8%%, LV2=±1.5%%, LV3=±2.5%% (VIX: 5/7/10%%)")
    log.info("yfinance: %s", "활성화" if _YF_READY else "비활성화")
    log.info("데이터 소스: %s", ", ".join(DATA_PROVIDERS))

    # 초기 1회 체크
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
