# market_watcher.py — FGPT Sentinel 시장감시 워커 (KOSPI 우선, 정규장 안정화)
# - 한국시장(KR): **KOSPI(^KS11) 1순위** → K200 ETF 평균(069500.KS, 102110.KS) → KOSPI200(^KS200)
#   · Alpha Vantage는 KR 세션과 거래시간이 어긋나는 자산(EWY 등)이라 **KR 세션에서는 사용하지 않음**(지연/괴리 방지)
#   · yfinance는 intraday 실시간성 확보를 위해 **fast_info 우선** 사용 (history 1d는 당일 반영 지연)
# - 미국시장(US): 기존 동일 (S&P500, NASDAQ, VIX / 장마감 시 선물 ES=F, NQ=F)
# - 알림 정책: 레벨이 "진입/변경/해제" 때만 전송
# - watch 주기: 기본 30분(WATCH_INTERVAL_SEC)
# - 전송 페이로드는 triggered_at(ISO 8601) 포함

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta

# -------------------- 설정/로그 --------------------
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
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "").strip()

def _env_int(key: str, default: int) -> int:
    import re
    v = os.getenv(key, str(default))
    m = re.search(r"\d+", v)
    return int(m.group()) if m else default

WATCH_INTERVAL = _env_int("WATCH_INTERVAL_SEC", 1800)
STATE_PATH     = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# 데이터 소스 설정
YF_ENABLED     = os.getenv("YF_ENABLED", "false").lower() in ("1", "true", "yes")
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS", "yahoo,yfinance").split(",") if s.strip()]
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()  # KR 세션에는 사용 안 함

# KR 심볼 (우선순위 체인)
KR_SYMBOLS = {
    "KOSPI": "^KS11",            # 1순위: 본지수(가장 안정)
    "K200_ETF1": "069500.KS",    # 2순위(평균1)
    "K200_ETF2": "102110.KS",    # 2순위(평균2)
    "K200": "^KS200",            # 3순위: KOSPI200 지수(추가 안전망)
}

# US 심볼
US = {
    "SPX": "^GSPC",
    "NDX": "^IXIC",
    "VIX": "^VIX",
    "ES":  "ES=F",
    "NQ":  "NQ=F",
}

COMMON_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://finance.yahoo.com/",
    "Cache-Control":   "no-cache",
}

# -------------------- 시간 유틸 --------------------

def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

# -------------------- 상태 --------------------

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

# -------------------- HTTP --------------------

def _http_get(url: str, params=None, timeout=12, max_retry=3):
    last = None
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            sc = getattr(e.response, "status_code", None)
            if sc in (401, 429, 500, 502, 503):
                last = e
                time.sleep(2 ** i)
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(1 + i)
            continue
    if last:
        raise last
    raise RuntimeError("HTTP 요청 실패")

# -------------------- Yahoo --------------------

def _yahoo_quote(symbols):
    syms = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": syms})
    return r.json().get("quoteResponse", {}).get("result", [])

def _extract_cp(q: dict):
    for k in ("regularMarketChangePercent", "changePercent"):
        if q.get(k) is not None:
            return float(q[k])
    p = q.get("regularMarketPrice") or q.get("price")
    prev = q.get("regularMarketPreviousClose") or q.get("previousClose")
    if p not in (None, 0) and prev not in (None, 0):
        return (float(p) - float(prev)) / float(prev) * 100.0
    return None

# -------------------- yfinance (intraday 우선) --------------------
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
        log.info("yfinance 활성화")
    except Exception as e:
        log.warning("yfinance 로드 실패: %s", e)

def _yf_cp(symbol: str) -> float:
    if not _YF_READY:
        raise RuntimeError("yfinance not ready")
    t = yf.Ticker(symbol)
    # fast_info를 최우선 사용 (intraday)
    fi = getattr(t, "fast_info", None)
    last = getattr(fi, "last_price", None) if fi else None
    prev = getattr(fi, "previous_close", None) if fi else None
    if last is not None and prev not in (None, 0):
        return (float(last) - float(prev)) / float(prev) * 100.0
    # 보조: 1분 캔들 시도 → 실패 시 일간
    try:
        hist = t.history(period="1d", interval="1m")
        if len(hist) >= 1 and "Close" in hist and fi and prev not in (None, 0):
            last = float(hist["Close"].iloc[-1])
            return (last - float(prev)) / float(prev) * 100.0
    except Exception:
        pass
    hist = t.history(period="2d", interval="1d")
    if len(hist) >= 2:
        prev = float(hist["Close"].iloc[-2])
        last = float(hist["Close"].iloc[-1])
        if prev != 0:
            return (last - prev) / prev * 100.0
    raise RuntimeError("yfinance insufficient")

# -------------------- KR Δ 수집 (KOSPI 우선) --------------------

def get_kr_delta() -> tuple[float, str]:
    # 1) KOSPI (^KS11)
    for prov in DATA_PROVIDERS:
        try:
            if prov == "yahoo":
                q = _yahoo_quote([KR_SYMBOLS["KOSPI"]])
                if q:
                    cp = _extract_cp(q[0])
                    if cp is not None:
                        return float(cp), "KOSPI(^KS11)"
            elif prov == "yfinance" and _YF_READY:
                cp = _yf_cp(KR_SYMBOLS["KOSPI"])
                return float(cp), "KOSPI(^KS11)"
        except Exception as e:
            log.debug("KOSPI via %s 실패: %s", prov, e)
            continue

    # 2) K200 ETF 평균 (069500.KS, 102110.KS)
    etfs = [KR_SYMBOLS["K200_ETF1"], KR_SYMBOLS["K200_ETF2"]]
    for prov in DATA_PROVIDERS:
        try:
            if prov == "yahoo":
                qs = _yahoo_quote(etfs)
                vals = []
                for it in qs:
                    cp = _extract_cp(it)
                    if cp is not None:
                        vals.append(cp)
                if vals:
                    return sum(vals) / len(vals), "ETF_AVG(069500,102110)"
            elif prov == "yfinance" and _YF_READY:
                vals = []
                for s in etfs:
                    try:
                        vals.append(_yf_cp(s))
                    except Exception:
                        pass
                if vals:
                    return sum(vals) / len(vals), "ETF_AVG(069500,102110)"
        except Exception as e:
            log.debug("ETF via %s 실패: %s", prov, e)
            continue

    # 3) K200 (^KS200)
    for prov in DATA_PROVIDERS:
        try:
            if prov == "yahoo":
                q = _yahoo_quote([KR_SYMBOLS["K200"]])
                if q:
                    cp = _extract_cp(q[0])
                    if cp is not None:
                        return float(cp), "K200(^KS200)"
            elif prov == "yfinance" and _YF_READY:
                cp = _yf_cp(KR_SYMBOLS["K200"])
                return float(cp), "K200(^KS200)"
        except Exception as e:
            log.debug("K200 via %s 실패: %s", prov, e)
            continue

    raise RuntimeError("KR Δ 수집 실패 (KOSPI/ETF/K200 모두 실패)")

# -------------------- US Δ 수집 --------------------

def _us_delta(symbol: str) -> float:
    for prov in DATA_PROVIDERS:
        try:
            if prov == "yahoo":
                q = _yahoo_quote([symbol])
                if q:
                    cp = _extract_cp(q[0])
                    if cp is not None:
                        return float(cp)
            elif prov == "yfinance" and _YF_READY:
                return _yf_cp(symbol)
        except Exception as e:
            log.debug("US %s via %s 실패: %s", symbol, prov, e)
            continue
    raise RuntimeError(f"US Δ 수집 실패: {symbol}")

# -------------------- 레벨링 --------------------

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

# -------------------- 알림 --------------------

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
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if not r.ok:
        log.error("알람 전송 실패 %s %s", r.status_code, r.text)
    else:
        log.info("알람 전송: %s %s %.2f%% (%s)", index_name, display_level, delta_pct or 0.0, note)

# -------------------- 세션 --------------------

def current_session() -> str:
    """KST 기준: KR(09:00~15:40), US(그 외) / 주말은 US"""
    now = _now_kst()
    if now.weekday() >= 5:
        return "US"
    hhmm = now.hour * 100 + now.minute
    return "KR" if 900 <= hhmm <= 1540 else "US"

def is_us_market_open() -> bool:
    now = _now_kst()
    m = now.month
    is_dst = 3 <= m <= 11
    h, mi = now.hour, now.minute
    if is_dst:
        if h == 22 and mi >= 30: return True
        return h >= 23 or h < 5
    else:
        if h == 23 and mi >= 30: return True
        return 0 <= h < 6

# -------------------- 메인 --------------------

def check_and_alert():
    state = _load_state()
    sess  = current_session()

    if sess == "KR":
        try:
            delta, src = get_kr_delta()
            lvl = grade_level(delta)
            prev = state.get("ΔKOSPI")
            if lvl != prev:
                note = "KR: 레벨 진입" if not prev else ("KR: 레벨 해제" if not lvl else f"KR: {prev}→{lvl}")
                post_alert("ΔKOSPI", delta, lvl, src, note)
                state["ΔKOSPI"] = lvl
            log.info("KR 체크: %s %.2f%% (레벨 %s)", src, delta, lvl or "정상")
        except Exception as e:
            log.error("KR Δ 수집/판정 실패: %s", e)

    else:
        open_now = is_us_market_open()
        pairs = (
            [("ΔSPX", US["SPX"], "US S&P500", False),
             ("ΔNASDAQ", US["NDX"], "US NASDAQ", False),
             ("ΔVIX", US["VIX"], "US VIX", True)] if open_now else
            [("ΔES", US["ES"], "US S&P500 선물", False),
             ("ΔNQ", US["NQ"], "US NASDAQ 선물", False)]
        )
        for key, sym, label, is_vix in pairs:
            try:
                d = _us_delta(sym)
                lvl = grade_level(d, is_vix=is_vix)
                prev = state.get(key)
                if lvl != prev:
                    note = f"{label}: 레벨 진입" if not prev else (f"{label}: 레벨 해제" if not lvl else f"{label}: {prev}→{lvl}")
                    post_alert(key, d, lvl, sym, note)
                    state[key] = lvl
                log.info("US 체크: %s %.2f%% (레벨 %s)", label, d, lvl or "정상")
            except Exception as e:
                log.warning("US %s 실패: %s", label, e)

    _save_state(state)


def run_loop():
    log.info("=== Sentinel 시장 감시 시작 ===")
    log.info("간격: %ds", WATCH_INTERVAL)
    log.info("KR: KOSPI 우선 → ETF 평균 → K200")
    log.info("US: S&P500, NASDAQ, VIX (마감 시 선물)")
    log.info("임계값: LV1=±0.8%%, LV2=±1.5%%, LV3=±2.5%% (VIX 5/7/10)")

    try:
        check_and_alert()
        log.info("초기 시장 체크 완료")
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
