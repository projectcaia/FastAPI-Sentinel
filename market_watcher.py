# market_watcher.py — FGPT Sentinel 시장감시 워커 (안정화 멀티소스 버전)
# - 주기: 기본 30분 (WATCH_INTERVAL_SEC=1800)
# - 알림 정책: 레벨이 "진입/변경/해제" 때만 전송
# - 세션: KST 주간(KR: ΔK200) / 야간(US: S&P500, NASDAQ, VIX) 자동 전환
# - 데이터 소스: yfinance → alphavantage → yahoo (체인 폴백)
# - Yahoo 401 회피: 헤더 보강 + query2 + 재시도/백오프
# - URL 보정: SENTINEL_BASE_URL에 스킴 미입력 시 https:// 자동 보정
#
# 필요 ENV
#   SENTINEL_BASE_URL=fastapi-sentinel-production.up.railway.app  # 스킴 없어도 됨(코드가 보정)
#   SENTINEL_KEY=sentinel_...
#   WATCH_INTERVAL_SEC=1800
#   LOG_LEVEL=INFO
#   BOLL_K_SIGMA=2.0
#   BOLL_WINDOW=20
#   WATCHER_STATE_PATH=./market_state.json
#   YF_ENABLED=true
#   DATA_PROVIDERS=yfinance,alphavantage,yahoo
#   ALPHAVANTAGE_API_KEY=YOUR_KEY
#   ALERT_CAP=2000
#
# 권장: SENTINEL_BASE_URL은 가능하면 https://포함 형태로 넣기

import os, time, json, logging, requests, math
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
K_SIGMA        = parse_float_env("BOLL_K_SIGMA", 2.5)  # 2.5 시그마로 상향 (노이즈 감소)
BB_WINDOW      = parse_int_env("BOLL_WINDOW", 20)

# 멀티소스 설정
YF_ENABLED     = os.getenv("YF_ENABLED", "false").lower() in ("1", "true", "yes")
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS", "yfinance,alphavantage,yahoo").split(",") if s.strip()]
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()

# 지수 → ETF 프록시 (Alpha Vantage는 지수 직접 조회 제약. ETF로 Δ% 대체)
AV_PROXY_MAP = {
    "^GSPC": "SPY",       # S&P500
    "^IXIC": "QQQ",       # NASDAQ100
    "^VIX":  "VIXY",      # VIX
    "^KS200": "069500.KS" # 필요 시 사용(국내는 yfinance/네이버 권장)
}

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

# -------------------- HTTP 유틸 (재시도/백오프) --------------------
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
            time.sleep(1.0 + i)
            last = e
            continue
    if last:
        raise last
    raise RuntimeError("HTTP 요청 실패(원인 미상)")

# -------------------- 데이터 소스: Yahoo --------------------
def _yahoo_quote(symbols):
    symbols_param = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    # query2로 전환
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": symbols_param}, timeout=12, max_retry=3)
    return r.json()

def _yahoo_chart(symbol: str, rng: str, interval: str):
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
        import yfinance as yf  # requirements.txt: yfinance>=0.2.40 필요
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance import 실패(YF_ENABLED=true): %s", e)

def _yf_change_percent(symbol: str):
    if not _YF_READY:
        raise RuntimeError("yfinance not ready")
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

# -------------------- 데이터 소스: Alpha Vantage (ETF 프록시) --------------------
def _alphavantage_change_percent(symbol: str) -> float:
    if not ALPHAVANTAGE_API_KEY:
        raise RuntimeError("ALPHAVANTAGE_API_KEY not set")
    sym = AV_PROXY_MAP.get(symbol, symbol)
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": sym, "apikey": ALPHAVANTAGE_API_KEY}
    r = _http_get(url, params=params, timeout=12, max_retry=2)
    data = r.json()
    # 레이트리밋/오류 메시지 대응
    if not isinstance(data, dict) or "Note" in data or "Information" in data:
        raise RuntimeError(f"AV rate/info: {data}")
    q = data.get("Global Quote") or data.get("globalQuote") or data
    if not isinstance(q, dict) or not q:
        raise RuntimeError(f"AV empty for {sym}")
    cp = q.get("10. change percent") or q.get("changePercent")
    if cp:
        try:
            return float(str(cp).strip().rstrip("%"))
        except Exception:
            pass
    price = q.get("05. price") or q.get("price")
    prev  = q.get("08. previous close") or q.get("previousClose")
    if price is not None and prev not in (None, "0", 0):
        price_f = float(price); prev_f = float(prev)
        if prev_f != 0:
            return (price_f - prev_f) / prev_f * 100.0
    raise RuntimeError(f"AV invalid quote for {sym}: {q}")

# -------------------- 프로바이더 체인 --------------------
def _provider_chain_get_change(symbols):
    last_err = None
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yfinance":
                if not _YF_READY:
                    raise RuntimeError("yfinance disabled/not ready")
                if isinstance(symbols, (list, tuple)):
                    vals = [_yf_change_percent(s) for s in symbols]
                    if vals:
                        return sum(vals) / len(vals)
                    raise RuntimeError("yfinance empty")
                else:
                    return _yf_change_percent(symbols)

            elif provider == "alphavantage":
                if isinstance(symbols, (list, tuple)):
                    vals = [_alphavantage_change_percent(s) for s in symbols]
                    if vals:
                        return sum(vals) / len(vals)
                    raise RuntimeError("alphavantage empty")
                else:
                    return _alphavantage_change_percent(symbols)

            elif provider == "yahoo":
                data = _yahoo_quote(symbols)
                items = data.get("quoteResponse", {}).get("result", [])
                if isinstance(symbols, (list, tuple)):
                    vals = []
                    for it in items:
                        cp = _extract_change_percent(it)
                        if cp is not None:
                            vals.append(float(cp))
                    if vals:
                        return sum(vals) / len(vals)
                    raise RuntimeError("yahoo empty")
                else:
                    if not items:
                        raise RuntimeError("yahoo empty")
                    cp = _extract_change_percent(items[0])
                    if cp is None:
                        raise RuntimeError("yahoo cp None")
                    return float(cp)
            else:
                raise RuntimeError(f"unknown provider {provider}")
        except Exception as e:
            last_err = e
            log.debug("provider %s failed: %s", provider, e)
            continue
    if last_err:
        raise last_err
    raise RuntimeError("no provider available")

# -------------------- Δ% 계산 --------------------
def get_delta(symbol) -> float:
    return float(_provider_chain_get_change([symbol]))

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
    raise RuntimeError("ΔK200 추정 실패")

# -------------------- 레벨링 --------------------
def grade_level(delta_pct: float, is_vix: bool = False) -> str | None:
    """레벨 판정 - VIX는 별도 기준 적용"""
    a = abs(delta_pct)
    
    if is_vix:
        # VIX는 변동성이 크므로 더 높은 임계값 적용
        if a >= 10.0: return "LV3"  # ±10% 이상
        if a >= 7.0: return "LV2"   # ±7% 이상
        if a >= 5.0: return "LV1"   # ±5% 이상
    else:
        # 일반 지수 (KOSPI, S&P500, NASDAQ)
        if a >= 2.5: return "LV3"   # ±2.5% 이상 (기존 1.5%에서 상향)
        if a >= 1.5: return "LV2"   # ±1.5% 이상 (기존 1.0%에서 상향)
        if a >= 0.8: return "LV1"   # ±0.8% 이상 (기존 0.4%에서 상향)
    return None

# -------------------- 알림 --------------------
def post_alert(index_name: str, delta_pct: float | None, level: str | None, source_tag: str, note: str):
    # level None → CLEARED
    display_level = level if level else "CLEARED"
    payload = {
        "index": index_name,
        "level": display_level,
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": f"{note} [{source_tag}]",
    }
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY

    url = f"{SENTINEL_BASE_URL}/sentinel/alert" if SENTINEL_BASE_URL else ""
    if not url.startswith("http"):
        # 추가 안전망 (베이스가 비어있거나 보정 실패한 경우)
        raise RuntimeError(f"SENTINEL_BASE_URL invalid: '{SENTINEL_BASE_URL}'")

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

# -------------------- 메인 감시 --------------------
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
        # US 세션: 먼저 S&P500과 NASDAQ 데이터 수집
        spx_delta = 0.0
        nasdaq_delta = 0.0
        
        try:
            spx_delta = get_delta(SYMBOL_SPX)
        except:
            pass
        
        try:
            nasdaq_delta = get_delta(SYMBOL_NDX)
        except:
            pass
        
        for idx_name, sym, label in [
            ("ΔSPX", SYMBOL_SPX,  "US 세션: S&P500"),
            ("ΔNASDAQ", SYMBOL_NDX, "US 세션: NASDAQ"),
            ("ΔVIX", SYMBOL_VIX,  "US 세션: VIX")
        ]:
            try:
                delta = get_delta(sym)
                is_vix = (sym == SYMBOL_VIX)
                
                # VIX 스마트 필터링: 지수가 크게 움직이지 않았는데 VIX만 튀면 무시
                if is_vix:
                    # S&P500이나 NASDAQ이 0.8% 미만 변동인데 VIX가 알림 레벨이면 무시
                    max_index_move = max(abs(spx_delta), abs(nasdaq_delta))
                    if max_index_move < 0.8:
                        # 지수 변동이 거의 없는데 VIX만 움직인 경우 건너뛰기
                        log.debug("VIX 스마트 필터: 지수 변동 %.2f%% 대비 VIX %.2f%% - 무시", max_index_move, delta)
                        state[idx_name] = None  # 상태 초기화
                        continue
                
                lvl = grade_level(delta, is_vix=is_vix)
                prev = state.get(idx_name)
                if lvl != prev:
                    note = f"{label} 레벨 변경" if prev else f"{label} 레벨 진입"
                    if prev and not lvl: note = f"{label} 레벨 해제"
                    elif prev and lvl:   note = f"{label} 레벨 변화 {prev}→{lvl}"
                    
                    # VIX의 경우 지수 변동도 함께 표시
                    if is_vix and lvl:
                        note += f" (S&P {spx_delta:+.2f}%, NAS {nasdaq_delta:+.2f}%)"
                    
                    post_alert(idx_name, delta, lvl, sym, note)
                    state[idx_name] = lvl
            except Exception as e:
                log.warning("%s 수집/판정 실패: %s", label, e)

    # Bollinger 이벤트 비활성화 (노이즈 방지)
    # try:
    #     state = check_and_alert_bb(state, sess)
    # except Exception as _bb_e:
    #     log.debug("BB 이벤트 처리 실패: %s", _bb_e)

    _save_state(state)

def run_loop():
    log.info("시장감시 워커 시작: 간격=%ss, base=%s", WATCH_INTERVAL, SENTINEL_BASE_URL or "(unset)")
    log.info("정책: %d초 유지, 레벨 변경시에만 업데이트, 한/미 자동 전환", WATCH_INTERVAL)
    log.info("볼린저 밴드: 비활성화 (노이즈 방지)")
    log.info("일반지수 임계값: LV1=±0.8%%, LV2=±1.5%%, LV3=±2.5%%")
    log.info("VIX 임계값: LV1=±5%%, LV2=±7%%, LV3=±10%%")
    log.info("VIX 스마트 필터: 지수 변동 0.8% 미만 시 VIX 알림 무시")
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
