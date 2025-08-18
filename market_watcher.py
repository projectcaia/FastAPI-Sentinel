# market_watcher.py — FGPT Sentinel 시장감시 워커 (강화판)
# - 주기: 기본 30분 (WATCH_INTERVAL_SEC=1800)
# - 알림 정책: 레벨이 "충족/강화/약화/해제" 될 때만 전송 (동일 레벨 유지 시 미전송)
# - 세션: KST 기준 주간(KR: ΔK200) / 야간(US: S&P500, NASDAQ, VIX) 자동 전환
# - Fallback: ΔK200은 ^KS200 → (069500.KS, 102110.KS) 평균 → ^KS11
# - Yahoo 401 회피: 헤더 보강 + query2 사용 + 재시도/백오프
# - 선택: yfinance 지원 (YF_ENABLED=true 설정 시)
# - 확장: DATA_PROVIDERS 환경변수로 소스 우선순위 지정 (예: "yahoo,yfinance")
#
# 필요 환경변수(.env):
#   SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app   # 끝에 / 없음
#   SENTINEL_KEY=sentinel_...                                              # web/worker 동일
#   WATCH_INTERVAL_SEC=1800
#   LOG_LEVEL=INFO
#   BOLL_K_SIGMA=2.0
#   BOLL_WINDOW=20
#   WATCHER_STATE_PATH=./market_state.json
#   # 선택:
#   YF_ENABLED=true                  # yfinance 사용 허용 (requirements.txt에 yfinance 추가)
#   DATA_PROVIDERS=yahoo,yfinance    # 우선순위 (쉼표 구분, 미설정 시 yahoo만)
#   ALERT_CAP=2000
#
# 상태 저장:
#   ./market_state.json 에 마지막 레벨 상태를 저장하여 레벨변경 감지

import os, time, json, logging, requests, math
from datetime import datetime, timezone, timedelta

# -------------------- 설정/로그 --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("market-watcher")

SENTINEL_BASE_URL = os.getenv("SENTINEL_BASE_URL", "https://fastapi-sentinel-production.up.railway.app").rstrip("/")
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "").strip()

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

WATCH_INTERVAL = parse_int_env("WATCH_INTERVAL_SEC", 1800)  # 30분 기본
STATE_PATH     = os.getenv("WATCHER_STATE_PATH", "./market_state.json")
K_SIGMA        = parse_float_env("BOLL_K_SIGMA", 2.0)
BB_WINDOW      = parse_int_env("BOLL_WINDOW", 20)

YF_ENABLED     = os.getenv("YF_ENABLED", "false").lower() in ("1", "true", "yes")
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS", "yahoo").split(",") if s.strip()]

# 공통 헤더 (Yahoo 401 회피)
COMMON_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://finance.yahoo.com/",
    "Origin":          "https://finance.yahoo.com",
    "Connection":      "keep-alive",
}

# 심볼 정의
SYMBOL_PRIMARY    = "^KS200"
SYMBOL_FALLBACKS  = ["069500.KS", "102110.KS", "^KS11"]
SYMBOL_SPX        = "^GSPC"
SYMBOL_NDX        = "^IXIC"
SYMBOL_VIX        = "^VIX"

def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))
def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

# -------------------- 상태 저장 --------------------
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

# -------------------- 데이터 소스: Yahoo --------------------
def _http_get(url: str, params=None, timeout=10, max_retry=3):
    last = None
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            # 401/429/5xx → 지수적 백오프 재시도
            if status in (401, 429) or (status and status >= 500):
                time.sleep(2 ** i)
                last = e
                continue
            raise
        except Exception as e:
            # 네트워크 등 → 소폭 재시도
            time.sleep(1.0 + i)
            last = e
            continue
    if last:
        raise last
    raise RuntimeError("HTTP 요청 실패(원인 미상)")

def _yahoo_quote(symbols):
    if isinstance(symbols, (list, tuple)):
        symbols_param = ",".join(symbols)
    else:
        symbols_param = symbols
    # query2로 전환
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": symbols_param}, timeout=12, max_retry=3)
    return r.json()

def _yahoo_chart(symbol: str, rng: str, interval: str):
    # chart도 query2 사용
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": rng, "interval": interval}
    r = _http_get(url, params=params, timeout=12, max_retry=3)
    data = r.json()
    res = data.get("chart", {}).get("result", [])
    if not res:
        raise RuntimeError(f"chart API no result for {symbol}")
    res0 = res[0]
    closes = res0.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    ts = res0.get("timestamp", [])
    series = [(t, c) for t, c in zip(ts, closes) if c is not None]
    if len(series) < BB_WINDOW + 2:
        raise RuntimeError(f"not enough points for {symbol} ({len(series)})")
    return [t for t, _ in series], [c for _, c in series]

def _extract_change_percent(q: dict):
    cp = q.get("regularMarketChangePercent")
    if cp is not None:
        return float(cp)
    price = q.get("regularMarketPrice")
    prev  = q.get("regularMarketPreviousClose")
    if price is not None and prev not in (None, 0):
        return (float(price) - float(prev)) / float(prev) * 100.0
    return None

# -------------------- 데이터 소스: yfinance (옵션) --------------------
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf  # requirements.txt: yfinance>=0.2.40 추가 필요
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance import 실패(YF_ENABLED=true): %s", e)

def _yf_change_percent(symbol: str):
    if not _YF_READY:
        raise RuntimeError("yfinance not ready")
    try:
        t = yf.Ticker(symbol)
        info = getattr(t, "fast_info", None)
        if info and getattr(info, "last_price", None) is not None and getattr(info, "previous_close", None) not in (None, 0):
            last = float(info.last_price)
            prev = float(info.previous_close)
            return (last - prev) / prev * 100.0
        # fallback: history
        hist = t.history(period="2d", interval="1d")
        if hist is not None and len(hist) >= 2:
            prev = float(hist["Close"].iloc[-2])
            last = float(hist["Close"].iloc[-1])
            return (last - prev) / prev * 100.0
        raise RuntimeError("yfinance insufficient data")
    except Exception as e:
        raise RuntimeError(f"yfinance fetch failed: {e}")

# -------------------- 심볼별 Δ% 계산 --------------------
def _provider_chain_get_change(symbols):
    """
    DATA_PROVIDERS 순서대로 시도.
    지원: yahoo, yfinance
    symbols: str or list
    """
    last_err = None
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                data = _yahoo_quote(symbols)
                items = data.get("quoteResponse", {}).get("result", [])
                if isinstance(symbols, (list, tuple)):
                    vals = []
                    for it in items:
                        cp = _extract_change_percent(it)
                        if cp is not None:
                            vals.append(float(cp))
                    if vals:
                        return sum(vals)/len(vals)
                    raise RuntimeError("yahoo: no valid change%")
                else:
                    if not items:
                        raise RuntimeError("yahoo: empty result")
                    cp = _extract_change_percent(items[0])
                    if cp is None:
                        raise RuntimeError("yahoo: change% none")
                    return float(cp)

            elif provider == "yfinance":
                if isinstance(symbols, (list, tuple)):
                    vals = []
                    for s in symbols:
                        vals.append(_yf_change_percent(s))
                    if vals:
                        return sum(vals)/len(vals)
                    raise RuntimeError("yfinance: no values")
                else:
                    return _yf_change_percent(symbols)

            # TODO: tradingview / investing / naver 플러그인 위치
            # elif provider == "tradingview": ...
            # elif provider == "investing": ...
            # elif provider == "naver": ...
            else:
                raise RuntimeError(f"unknown provider {provider}")

        except Exception as e:
            last_err = e
            log.debug("provider %s failed: %s", provider, e)
            continue
    if last_err:
        raise last_err
    raise RuntimeError("no provider available")

def get_delta_k200() -> tuple[float, str]:
    # 1) ^KS200
    try:
        cp = _provider_chain_get_change([SYMBOL_PRIMARY])
        return float(cp), SYMBOL_PRIMARY
    except Exception as e:
        log.warning("primary ^KS200 실패: %s", e)

    # 2) ETF 평균
    try:
        cp = _provider_chain_get_change(SYMBOL_FALLBACKS[:2])
        return float(cp), "ETF_AVG(069500.KS,102110.KS)"
    except Exception as e:
        log.warning("ETF 평균 실패: %s", e)

    # 3) KOSPI 본지수
    try:
        cp = _provider_chain_get_change([SYMBOL_FALLBACKS[-1]])
        return float(cp), SYMBOL_FALLBACKS[-1]
    except Exception as e:
        log.warning("KOSPI 본지수 ^KS11 실패: %s", e)

    raise RuntimeError("ΔK200 추정 실패: 모든 소스에서 데이터 없음")

def get_delta(symbol) -> float:
    cp = _provider_chain_get_change([symbol])
    return float(cp)

def grade_level(delta_pct: float) -> str | None:
    a = abs(delta_pct)
    if a >= 1.5: return "LV3"
    if a >= 1.0: return "LV2"
    if a >= 0.4: return "LV1"
    return None

# -------------------- 알림 --------------------
def post_alert(index_name: str, delta_pct: float, level: str | None, source_tag: str, note: str):
    # level이 None이면 '해제' 알림으로 처리, 볼린저는 특수 레벨 사용
    if level in ["BREACH", "RECOVER", "CLEARED"]:
        display_level = "LV2" if level != "CLEARED" else level
        note = f"[BB {level}] " + note
    else:
        display_level = level if level else "CLEARED"

    payload = {
        "index": index_name,
        "level": display_level,
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": note + f" [{source_tag}]",
    }
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if not r.ok:
        log.error("알람 전송 실패 %s %s", r.status_code, r.text)
    else:
        log.info("알람 전송: %s %s %.2f%% (%s)", index_name, payload["level"], payload["delta_pct"] or 0.0, note)

# -------------------- 세션 판별 --------------------
def current_session() -> str:
    """KST 기준: KR(08:30~16:00), US(그 외) / 주말은 US 처리"""
    now = _now_kst()
    hhmm = now.hour * 100 + now.minute
    if now.weekday() >= 5:
        return "US"
    return "KR" if 830 <= hhmm <= 1600 else "US"

# -------------------- Bollinger(±2σ) --------------------
def _zscore_latest(closes: list[float], window: int) -> tuple[float, float]:
    if len(closes) < window + 1:
        raise RuntimeError("insufficient closes for zscore")
    base = closes[-(window+1):-1]
    mu = sum(base) / len(base)
    variance = sum((x - mu) ** 2 for x in base) / (len(base) - 1)
    sd = math.sqrt(variance) if variance > 0 else 0.0
    if sd == 0: return 0.0, 0.0
    last, prev = closes[-1], closes[-2]
    return (last - mu) / sd, (prev - mu) / sd

def bb_event_for_symbol(symbol: str, rngs=("5d","1mo"), intervals=("30m","60m","1d")):
    last_err = None
    for rng in rngs:
        for iv in intervals:
            try:
                _, closes = _yahoo_chart(symbol, rng, iv)
                z, z_prev = _zscore_latest(closes, BB_WINDOW)
                breach_now  = abs(z) >= K_SIGMA
                breach_prev = abs(z_prev) >= K_SIGMA
                if breach_now and not breach_prev: return "BREACH", z, f"{symbol}:{rng}/{iv}"
                if (not breach_now) and breach_prev: return "RECOVER", z, f"{symbol}:{rng}/{iv}"
                return None, z, f"{symbol}:{rng}/{iv}"
            except Exception as e:
                last_err = e
                continue
    if last_err: raise last_err
    return None, 0.0, f"{symbol}:n/a"

def check_and_alert_bb(state: dict, sess: str):
    if sess == "KR":
        for sym in [SYMBOL_PRIMARY, "069500.KS", "102110.KS", "^KS11"]:
            try:
                ev, z, used = bb_event_for_symbol(sym)
                prev = state.get("BB_ΔK200")
                if ev and ev != prev:
                    note = f"KR 세션: Bollinger ±{K_SIGMA}σ {'돌파' if ev=='BREACH' else '회복'} (z={z:.2f})"
                    post_alert("ΔK200_VOL", round(z,2), ev, used, note)
                    state["BB_ΔK200"] = ev
                break
            except Exception as e:
                log.debug("KR BB 후보 실패 %s: %s", sym, e)
                continue
    else:
        for state_key, idx_name, sym, label in [
            ("BB_ΔSPX","ΔSPX_VOL", SYMBOL_SPX, "US S&P500"),
            ("BB_ΔNASDAQ","ΔNASDAQ_VOL", SYMBOL_NDX, "US NASDAQ"),
            ("BB_ΔVIX","ΔVIX_VOL", SYMBOL_VIX, "US VIX")
        ]:
            try:
                ev, z, used = bb_event_for_symbol(sym)
                prev = state.get(state_key)
                if ev and ev != prev:
                    note = f"{label}: Bollinger ±{K_SIGMA}σ {'돌파' if ev=='BREACH' else '회복'} (z={z:.2f})"
                    post_alert(idx_name, round(z,2), ev, used, note)
                    state[state_key] = ev
            except Exception as e:
                log.debug("US BB 실패 %s: %s", sym, e)
                continue
    return state

# -------------------- 메인 감시 루프 --------------------
def grade_level(delta_pct: float) -> str | None:
    a = abs(delta_pct)
    if a >= 1.5: return "LV3"
    if a >= 1.0: return "LV2"
    if a >= 0.4: return "LV1"
    return None

def check_and_alert():
    state = _load_state()

    sess = current_session()
    if sess == "KR":
        try:
            delta, tag = get_delta_k200()
            lvl = grade_level(delta)
            prev = state.get("ΔK200")
            if lvl != prev:
                note = "KR 세션: 레벨 변경" if prev else "KR 세션: 레벨 진입"
                if prev and not lvl: note = "KR 세션: 레벨 해제"
                elif prev and lvl:   note = f"KR 세션: 레벨 변화 {prev}→{lvl}"
                post_alert("ΔK200", delta, lvl, tag, note)
                state["ΔK200"] = lvl
        except Exception as e:
            log.warning("KR ΔK200 수집/판정 실패: %s", e)
    else:
        for idx_name, sym, label in [
            ("ΔSPX", SYMBOL_SPX,  "US 세션: S&P500"),
            ("ΔNASDAQ", SYMBOL_NDX, "US 세션: NASDAQ"),
            ("ΔVIX", SYMBOL_VIX,  "US 세션: VIX")
        ]:
            try:
                delta = get_delta(sym)
                lvl = grade_level(delta)
                prev = state.get(idx_name)
                if lvl != prev:
                    note = f"{label} 레벨 변경" if prev else f"{label} 레벨 진입"
                    if prev and not lvl: note = f"{label} 레벨 해제"
                    elif prev and lvl:   note = f"{label} 레벨 변화 {prev}→{lvl}"
                    post_alert(idx_name, delta, lvl, sym, note)
                    state[idx_name] = lvl
            except Exception as e:
                log.warning("%s 수집/판정 실패: %s", label, e)

    # Bollinger 이벤트 병행 감지
    try:
        state = check_and_alert_bb(state, sess)
    except Exception as _bb_e:
        log.debug("BB 이벤트 처리 실패: %s", _bb_e)

    _save_state(state)

def run_loop():
    log.info("시장감시 워커 시작: interval=%ss, base=%s", WATCH_INTERVAL, SENTINEL_BASE_URL)
    log.info("정책: %d초 유지, 레벨 변경시에만 업데이트, 한/미 자동 전환", WATCH_INTERVAL)
    log.info("볼린저 밴드: ±%.1fσ 기준, %d기간 이동평균", K_SIGMA, BB_WINDOW)
    # 초기 즉시 체크
    try:
        log.info("초기 시장 체크 실행...")
        check_and_alert()
        log.info("초기 시장 체크 완료")
    except Exception as e:
        log.error("초기 체크 실패: %s", e)
    # 주기 루프
    while True:
        try:
            check_and_alert()
        except Exception as e:
            log.error("주기 실행 오류: %s", e)
        time.sleep(WATCH_INTERVAL)

if __name__ == "__main__":
    run_loop()
