# market_watcher.py — FGPT Sentinel 시장감시 워커 (정규장 안정 + 선물 PRE 0.8% 임계)
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
WATCH_INTERVAL = parse_int_env("WATCH_INTERVAL_SEC", 1800)                 # 30분
MAX_STALENESS_SEC = parse_int_env("MAX_STALENESS_SEC", 900)                # 현물 신선도(15분)
FUT_INTRADAY_MAX_AGE_SEC = parse_int_env("FUT_INTRADAY_MAX_AGE_SEC", 600)  # 선물 캔들 신선도(10분)
FUT_PRE_ALERT_THRESHOLD = parse_float_env("FUT_PRE_ALERT_THRESHOLD", 0.8)  # 선물 PRE 임계값(%) |Δ|≥0.8만 알림

# 선물 블랙아웃(Globex maint.) KST
FUT_BLACKOUT_START = os.getenv("FUT_BLACKOUT_START_KST", "06:00")
FUT_BLACKOUT_END   = os.getenv("FUT_BLACKOUT_END_KST",   "07:00")

STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# 멀티소스(현물 체인 전용)
YF_ENABLED = os.getenv("YF_ENABLED", "true").lower() in ("1", "true", "yes")
_RAW_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS", "yahoo,yfinance").split(",") if s.strip()]
_PREFERRED = {"yahoo": 0, "yfinance": 1, "alphavantage": 2}
DATA_PROVIDERS = sorted(set(_RAW_PROVIDERS), key=lambda x: _PREFERRED.get(x, 99))

ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()  # 현물 보조용(선물엔 사용 안 함)

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

# 현물/선물 심볼
KR_SPOT_PRIORITY = ["069500.KS", "^KS200", "^KS11"]   # KODEX200 → KS200 → KOSPI
US_SPOT = ["^GSPC", "^IXIC", "^VIX"]
ES_FUT, NQ_FUT = "ES=F", "NQ=F"

# ==================== 공통 유틸 ====================
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def _utc_ts_now() -> float:
    return datetime.now(timezone.utc).timestamp()

def _utc_to_kst(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(timezone(timedelta(hours=9)))

def _in_time_range_kst(start_str: str, end_str: str) -> bool:
    """KST 시각이 [start,end) 범위인지"""
    now = _now_kst()
    s_h, s_m = map(int, start_str.split(":"))
    e_h, e_m = map(int, end_str.split(":"))
    start = now.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
    end   = now.replace(hour=e_h, minute=e_m, second=0, microsecond=0)
    return start <= now < end

# 상태 파일
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

# ==================== HTTP / Yahoo quote (현물 전용) ====================
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

def _yahoo_quote(symbols):
    symbols_param = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": symbols_param})
    return r.json()

def _pick_price_fields(q: dict):
    price = q.get("regularMarketPrice") or q.get("price")
    prev  = q.get("regularMarketPreviousClose") or q.get("previousClose")
    state = q.get("marketState")  # "REGULAR","CLOSED","PRE","POST"
    ts = q.get("regularMarketTime") or q.get("postMarketTime") or q.get("preMarketTime")
    try: price = float(price)
    except: price = None
    try: prev = float(prev)
    except: prev = None
    try: ts = float(ts)
    except: ts = None
    return price, prev, state, ts

def _is_fresh(ts_epoch: float | None, max_age_sec: int) -> bool:
    if not ts_epoch:
        return False
    return (_utc_ts_now() - float(ts_epoch)) <= max_age_sec

def _extract_change_percent(q: dict, require_regular: bool = False, require_fresh: bool = False):
    price, prev, state, ts = _pick_price_fields(q)
    if require_regular and state and state != "REGULAR":
        return None
    if require_fresh and not _is_fresh(ts, MAX_STALENESS_SEC):
        return None
    if price is not None and prev not in (None, 0.0):
        return (price - prev) / prev * 100.0
    cp = q.get("regularMarketChangePercent")
    try:
        return float(cp) if cp is not None else None
    except:
        return None

# ==================== yfinance (선물 강제 + 현물 보조) ====================
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance import 실패: %s", e)

def _yf_intraday_last(symbol: str, prefer_1m=True):
    """
    선물용: 최근 캔들의 (종가, KST타임스탬프) 반환.
    1m 실패 시 5m 폴백. 없으면 (None, None).
    """
    if not _YF_READY:
        return None, None
    try:
        interval = "1m" if prefer_1m else "5m"
        t = yf.Ticker(symbol)
        df = t.history(period="1d", interval=interval, prepost=True, actions=False)
        if df is None or len(df) == 0:
            if prefer_1m:
                return _yf_intraday_last(symbol, prefer_1m=False)
            return None, None
        last_ts_utc = df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc).timestamp()
        last_close = float(df["Close"].iloc[-1])
        return last_close, _utc_to_kst(last_ts_utc)
    except Exception as e:
        log.debug("yfinance intraday 실패(%s): %s", symbol, e)
        if prefer_1m:
            return _yf_intraday_last(symbol, prefer_1m=False)
        return None, None

def _yf_prev_close(symbol: str):
    if not _YF_READY:
        return None
    try:
        t = yf.Ticker(symbol)
        info = getattr(t, "fast_info", None)
        prev = None
        if info:
            prev = getattr(info, "previous_close", None) or getattr(info, "regularMarketPreviousClose", None)
        if prev is None:
            df = t.history(period="2d", interval="1d", prepost=True, actions=False)
            if df is not None and len(df) >= 2:
                prev = float(df["Close"].iloc[-2])
        return float(prev) if prev is not None else None
    except Exception as e:
        log.debug("yfinance prev_close 실패(%s): %s", symbol, e)
        return None

def futures_change_pct(symbol: str) -> tuple[float | None, dict]:
    """
    선물 변화율 = (현재가(최근 캔들 종가) - 전일 정산가) / 전일 정산가 * 100
    신선도: 최근 캔들이 FUT_INTRADAY_MAX_AGE_SEC 이내여야 유효.
    블랙아웃: FUT_BLACKOUT_* 구간은 무시.
    """
    if _in_time_range_kst(FUT_BLACKOUT_START, FUT_BLACKOUT_END):
        return None, {"err": "blackout"}
    price, kst_ts = _yf_intraday_last(symbol)
    if price is None or kst_ts is None:
        return None, {"err": "no_intraday"}
    if (_now_kst() - kst_ts).total_seconds() > max(60, FUT_INTRADAY_MAX_AGE_SEC):
        return None, {"err": "stale_intraday", "last": kst_ts.isoformat(timespec="seconds")}
    prev = _yf_prev_close(symbol)
    if prev in (None, 0.0):
        return None, {"err": "no_prev_close"}
    try:
        cp = (price - prev) / prev * 100.0
        return cp, {"last": kst_ts.isoformat(timespec="seconds"), "prev": round(prev, 4), "price": round(price, 4)}
    except Exception as e:
        return None, {"err": f"calc_fail: {e}"}

# ==================== Alpha Vantage (현물 보조) ====================
def _alphavantage_change_percent(symbol: str) -> float:
    if not ALPHAVANTAGE_API_KEY:
        raise RuntimeError("ALPHAVANTAGE_API_KEY not set")
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHAVANTAGE_API_KEY}
    r = _http_get(url, params=params)
    data = r.json()
    q = data.get("Global Quote", {})
    cp = q.get("10. change percent")
    if cp:
        try:
            return float(str(cp).strip().rstrip("%"))
        except:
            pass
    price = q.get("05. price"); prev = q.get("08. previous close")
    if price and prev and float(prev) != 0:
        return (float(price) - float(prev)) / float(prev) * 100.0
    raise RuntimeError(f"AV invalid quote for {symbol}")

# ==================== 세션/시간 ====================
def current_session() -> str:
    """KST 기준 세션 판별"""
    now = _now_kst()
    hhmm = now.hour * 100 + now.minute
    if now.weekday() >= 5:
        return "US"
    return "KR" if 830 <= hhmm <= 1600 else "US"

def is_us_market_open() -> bool:
    """미국 정규장 시간 체크 (KST, 서머타임 단순화)"""
    now = _now_kst()
    hour, minute, month = now.hour, now.minute, now.month
    is_dst = 3 <= month <= 11
    if is_dst:   # 22:30 ~ 05:00
        if hour == 22 and minute >= 30:
            return True
        return (23 <= hour) or (hour < 5)
    else:        # 23:30 ~ 06:00
        if hour == 23 and minute >= 30:
            return True
        return 0 <= hour < 6

# ==================== 프로바이더 체인 (현물 전용) ====================
def _provider_chain_get_change_spot(symbols, require_regular=True, require_fresh=True):
    """
    현물 지수 변화율 수집: yahoo → yfinance → alphavantage
    (선물은 여기로 오지 않음!)
    """
    last_err = None
    if not isinstance(symbols, (list, tuple)):
        symbols = [symbols]

    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                data = _yahoo_quote(symbols)
                items = data.get("quoteResponse", {}).get("result", [])
                vals = []
                for i, sym in enumerate(symbols):
                    if i >= len(items):
                        continue
                    cp = _extract_change_percent(items[i], require_regular=require_regular, require_fresh=require_fresh)
                    if cp is not None and not math.isnan(cp):
                        vals.append(float(cp))
                if vals:
                    return sum(vals) / len(vals)
                raise RuntimeError("yahoo no valid fresh data")

            elif provider == "yfinance" and _YF_READY:
                vals = []
                for sym in symbols:
                    try:
                        t = yf.Ticker(sym)
                        info = getattr(t, "fast_info", None)
                        if info:
                            last = getattr(info, "regularMarketPrice", None) or getattr(info, "last_price", None)
                            prev = getattr(info, "regularMarketPreviousClose", None) or getattr(info, "previous_close", None)
                            if last is not None and prev not in (None, 0):
                                vals.append((float(last) - float(prev)) / float(prev) * 100.0)
                    except:
                        continue
                if vals:
                    return sum(vals) / len(vals)
                raise RuntimeError("yfinance no valid data")

            elif provider == "alphavantage" and ALPHAVANTAGE_API_KEY:
                vals = []
                for sym in symbols:
                    try:
                        vals.append(_alphavantage_change_percent(sym))
                    except:
                        continue
                if vals:
                    return sum(vals) / len(vals)
                raise RuntimeError("alphavantage no valid data")

        except Exception as e:
            last_err = e
            log.debug("provider %s failed: %s", provider, e)
            continue

    if last_err:
        raise last_err
    raise RuntimeError("no provider available")

# ==================== 변화율 수집 래퍼 ====================
def get_delta_spot(symbol) -> float:
    """정규장 현물 지수 변동률(신선도·REGULAR 필수)"""
    return float(_provider_chain_get_change_spot([symbol], True, True))

def get_delta_k200() -> tuple[float, str]:
    for sym in KR_SPOT_PRIORITY:
        try:
            return float(_provider_chain_get_change_spot([sym], True, True)), sym
        except Exception as e:
            log.warning("%s 실패: %s", human_name(sym), e)
    raise RuntimeError("KR 현물 데이터 수집 실패")

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
    """
    kind: "ALERT"(정규장 현물), "PRE"(야간 선물 경계), "INFO"
    """
    display_name = human_name(source_tag)
    payload = {
        "index": display_name,
        "level": level or ("PRE" if kind == "PRE" else "CLEARED"),
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
            log.info("알림 전송: [%s] %s %s %.2f%% (%s)", kind, display_name, level or "CLEARED", delta_pct or 0, note)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# ==================== 메인 감시 ====================
def check_and_alert():
    state = _load_state()
    sess = current_session()
    log.info("===== 시장 체크 [%s 세션] =====", "KR" if sess == "KR" else "US")
    state["last_checked_at"] = _now_kst_iso()

    if sess == "KR":
        # 한국 정규장: 현물 경보만
        try:
            delta, tag = get_delta_k200()
            lvl = grade_level(delta)
            prev = state.get("ΔK200")
            name = human_name(tag)
            log.info("%s: %.2f%% [%s → %s]", name, delta, prev or "없음", lvl or "정상")
            if lvl != prev:
                note = ("레벨 진입" if (not prev and lvl) else
                        "레벨 해제" if (prev and not lvl) else
                        f"{prev} → {lvl}")
                post_alert(delta, lvl, tag, note, kind="ALERT")
                state["ΔK200"] = lvl
        except Exception as e:
            log.warning("KR 수집 실패: %s", e)

    else:
        # 미국 세션
        us_open = is_us_market_open()
        if us_open:
            # US 정규장: 현물 경보만
            # VIX 필터를 위해 S&P/NAS 선행 수집
            try:
                spx = get_delta_spot("^GSPC")
            except Exception as e:
                log.debug("S&P 500 선행 실패: %s", e); spx = 0.0
            try:
                ndx = get_delta_spot("^IXIC")
            except Exception as e:
                log.debug("NASDAQ 선행 실패: %s", e); ndx = 0.0

            for sym, is_vix in [("^GSPC", False), ("^IXIC", False), ("^VIX", True)]:
                try:
                    cp = get_delta_spot(sym)
                    if is_vix:
                        max_move = max(abs(spx), abs(ndx))
                        if max_move < 0.8:
                            log.debug("VIX 필터: 지수 %.2f/%.2f%% 대비 %.2f%% → 무시", spx, ndx, cp)
                            state["ΔVIX"] = None
                            continue
                    lvl = grade_level(cp, is_vix=is_vix)
                    key = "ΔVIX" if is_vix else ("ΔSPX" if sym == "^GSPC" else "ΔNASDAQ")
                    prev = state.get(key)
                    name = human_name(sym)
                    log.info("%s: %.2f%% [%s → %s]", name, cp, prev or "없음", lvl or "정상")
                    if lvl != prev:
                        note = ("레벨 진입" if (not prev and lvl) else
                                "레벨 해제" if (prev and not lvl) else
                                f"{prev} → {lvl}")
                        if is_vix and lvl:
                            note += f" (S&P {spx:+.2f}%, NAS {ndx:+.2f}%)"
                        post_alert(cp, lvl, sym, note, kind="ALERT")
                        state[key] = lvl
                except Exception as e:
                    log.warning("%s 수집 실패: %s", human_name(sym), e)
        else:
            # US 미개장: 선물 PRE(경계)만, 레벨 경보 금지 (VIX 제외)
            for key, sym in [("ΔES_PRE", ES_FUT), ("ΔNQ_PRE", NQ_FUT)]:
                name = human_name(sym)
                cp, meta = futures_change_pct(sym)
                if cp is None:
                    reason = meta.get("err", "unknown")
                    if reason == "blackout":
                        log.info("%s: 블랙아웃(%s~%s) 스킵", name, FUT_BLACKOUT_START, FUT_BLACKOUT_END)
                    else:
                        log.info("%s: 데이터 없음/지연(%s)", name, reason)
                    state[key] = None
                    continue

                # ★ 임계값: |변동률| ≥ 0.8%일 때만 PRE 알림
                if abs(cp) < FUT_PRE_ALERT_THRESHOLD:
                    log.info("%s: %.2f%% (임계값 %.1f%% 미만 → PRE 알림 생략)", name, cp, FUT_PRE_ALERT_THRESHOLD)
                    state[key] = cp
                    continue

                # 노이즈 억제: 이전 값과 0.1%p 미만이면 중복 알림 생략
                prev_val = state.get(key)
                if prev_val is not None:
                    try:
                        if abs(cp - float(prev_val)) < 0.1:
                            log.info("%s: %.2f%% (변화 0.1%%p 미만 → PRE 알림 생략)", name, cp)
                            state[key] = cp
                            continue
                    except Exception:
                        pass

                note = "야간 선물 변동(전일 정산가 대비)"
                if "last" in meta:
                    note += f" | last={meta['last']}"
                post_alert(cp, level="PRE", source_tag=sym, note=note, kind="PRE")
                log.info("%s: %.2f%% [PRE 알림]", name, cp)
                state[key] = cp

    _save_state(state)
    log.info("===== 체크 완료 =====")

# ==================== 메인 루프 ====================
def run_loop():
    log.info("=== Sentinel 시장감시 시작 ===")
    log.info("간격: %d초", WATCH_INTERVAL)
    log.info("데이터 소스(현물 체인 우선순위): %s", ", ".join(DATA_PROVIDERS))
    log.info("임계값 - 일반: 0.8%/1.5%/2.5%, VIX: 5%/7%/10%")
    log.info("신선도(현물): %ds, 선물 캔들 신선도: %ds, 선물 PRE 임계: %.1f%%, 블랙아웃: %s~%s KST",
             MAX_STALENESS_SEC, FUT_INTRADAY_MAX_AGE_SEC, FUT_PRE_ALERT_THRESHOLD, FUT_BLACKOUT_START, FUT_BLACKOUT_END)
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
