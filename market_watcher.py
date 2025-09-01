# market_watcher.py — FGPT Sentinel 시장감시 워커 (실시간·신선도 + 사람친화 지표명)
# -*- coding: utf-8 -*-

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

WATCH_INTERVAL = parse_int_env("WATCH_INTERVAL_SEC", 1800)  # 30분 기본
STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")
MAX_STALENESS_SEC = parse_int_env("MAX_STALENESS_SEC", 900)  # 15분 이내만 유효

# 멀티소스 설정
YF_ENABLED = os.getenv("YF_ENABLED", "false").lower() in ("1", "true", "yes")
_RAW_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS", "yahoo,yfinance,alphavantage").split(",") if s.strip()]
_PREFERRED = {"yahoo": 0, "yfinance": 1, "alphavantage": 2}
DATA_PROVIDERS = sorted(set(_RAW_PROVIDERS), key=lambda x: _PREFERRED.get(x, 99))
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()

# Alpha Vantage ETF 프록시
AV_PROXY_MAP = {
    "^GSPC": "SPY",
    "^IXIC": "QQQ",
    "^VIX":  "VIXY",
    "^KS200":"069500.KS"
}

# 심볼 정의
SYMBOL_PRIMARY = "^KS200"
SYMBOL_FALLBACKS = ["069500.KS", "102110.KS", "^KS11"]
SYMBOL_SPX = "^GSPC"
SYMBOL_NDX = "^IXIC"
SYMBOL_VIX = "^VIX"
SYMBOL_SPX_FUT = "ES=F"
SYMBOL_NDX_FUT = "NQ=F"

# === 사람친화 지표명 매핑 ===
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

def human_name(symbol_or_tag: str, fallback: str | None = None) -> str:
    return HUMAN_NAMES.get(symbol_or_tag, fallback or symbol_or_tag)

def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def _utc_to_kst(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(timezone(timedelta(hours=9)))

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

# -------------------- HTTP 유틸 --------------------
H_COMMON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
}
def _http_get(url: str, params=None, timeout=10, max_retry=3):
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

# -------------------- 야후 파이낸스 --------------------
def _yahoo_quote(symbols):
    symbols_param = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = _http_get(url, params={"symbols": symbols_param}, timeout=12, max_retry=3)
    return r.json()

def _pick_price_fields(q: dict):
    price = q.get("regularMarketPrice") or q.get("price")
    prev  = q.get("regularMarketPreviousClose") or q.get("previousClose")
    state = q.get("marketState")
    ts = q.get("regularMarketTime") or q.get("postMarketTime") or q.get("preMarketTime")
    try: price = float(price)
    except: price = None
    try: prev = float(prev)
    except: prev = None
    try: ts = float(ts)
    except: ts = None
    return price, prev, state, ts

def _is_fresh(ts_epoch: float | None, max_age_sec: int) -> bool:
    if not ts_epoch: return False
    now = datetime.now(timezone.utc).timestamp()
    return (now - float(ts_epoch)) <= max_age_sec

def _extract_change_percent(q: dict, symbol: str = None, require_regular: bool = False, require_fresh: bool = False):
    price, prev, state, ts = _pick_price_fields(q)
    if require_regular and state and state != "REGULAR":
        return None
    if require_fresh and not _is_fresh(ts, MAX_STALENESS_SEC):
        return None
    if price is not None and prev not in (None, 0.0):
        return (price - prev) / prev * 100.0
    cp = q.get("regularMarketChangePercent")
    return float(cp) if cp is not None else None

# -------------------- yfinance --------------------
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
    except Exception:
        pass
def _yf_change_percent(symbol: str):
    if not _YF_READY:
        raise RuntimeError("yfinance not ready")
    t = yf.Ticker(symbol)
    info = getattr(t, "fast_info", None)
    if info:
        last = getattr(info, "regularMarketPrice", None) or getattr(info, "last_price", None)
        prev = getattr(info, "regularMarketPreviousClose", None) or getattr(info, "previous_close", None)
        if last is not None and prev not in (None, 0):
            return (float(last) - float(prev)) / float(prev) * 100.0
    hist = t.history(period="2d", interval="1d")
    if hist is not None and len(hist) >= 2:
        prev = float(hist["Close"].iloc[-2])
        last = float(hist["Close"].iloc[-1])
        if prev != 0:
            return (last - prev) / prev * 100.0
    raise RuntimeError("yfinance insufficient data")

# -------------------- Alpha Vantage --------------------
def _alphavantage_change_percent(symbol: str) -> float:
    if not ALPHAVANTAGE_API_KEY:
        raise RuntimeError("ALPHAVANTAGE_API_KEY not set")
    sym = AV_PROXY_MAP.get(symbol, symbol)
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": sym, "apikey": ALPHAVANTAGE_API_KEY}
    r = _http_get(url, params=params, timeout=12, max_retry=2)
    data = r.json()
    q = data.get("Global Quote", {})
    cp = q.get("10. change percent")
    if cp:
        return float(str(cp).rstrip("%"))
    price = q.get("05. price"); prev = q.get("08. previous close")
    if price and prev and float(prev) != 0:
        return (float(price) - float(prev)) / float(prev) * 100.0
    raise RuntimeError(f"AV invalid quote for {sym}")

# -------------------- 세션/시간 --------------------
def current_session() -> str:
    now = _now_kst(); hhmm = now.hour*100 + now.minute
    if now.weekday() >= 5: return "US"
    return "KR" if 830 <= hhmm <= 1600 else "US"
def is_us_market_open() -> bool:
    now = _now_kst(); hour, minute, month = now.hour, now.minute, now.month
    is_dst = 3 <= month <= 11
    if is_dst: return (hour==22 and minute>=30) or (23<=hour or hour<5)
    else:      return (hour==23 and minute>=30) or (0<=hour<6)

# -------------------- 프로바이더 체인 --------------------
def _provider_chain_get_change(symbols, require_regular_by_symbol=False, require_fresh_by_symbol=False):
    last_err = None
    if not isinstance(symbols, (list, tuple)): symbols = [symbols]
    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                data = _yahoo_quote(symbols)
                items = data.get("quoteResponse", {}).get("result", [])
                vals = []
                for idx, sym in enumerate(symbols):
                    if idx >= len(items): continue
                    cp = _extract_change_percent(items[idx], sym, require_regular_by_symbol, require_fresh_by_symbol)
                    if cp is not None: vals.append(float(cp))
                if vals: return sum(vals)/len(vals)
            elif provider == "yfinance" and _YF_READY:
                vals=[]
                for sym in symbols:
                    try: vals.append(_yf_change_percent(sym))
                    except: continue
                if vals: return sum(vals)/len(vals)
            elif provider == "alphavantage" and ALPHAVANTAGE_API_KEY:
                vals=[]
                for sym in symbols:
                    try: vals.append(_alphavantage_change_percent(sym))
                    except: continue
                if vals: return sum(vals)/len(vals)
        except Exception as e:
            last_err = e; continue
    if last_err: raise last_err
    raise RuntimeError("no provider available")

# -------------------- 변화율 수집 --------------------
def get_delta(symbol) -> float:
    require_regular = require_fresh = False
    if symbol in (SYMBOL_SPX, SYMBOL_NDX, SYMBOL_VIX): require_regular=require_fresh=True
    if ".KS" in symbol or "^KS" in symbol: require_regular=require_fresh=True
    return float(_provider_chain_get_change([symbol], require_regular, require_fresh))
def get_delta_k200() -> tuple[float,str]:
    for sym in ["069500.KS", SYMBOL_PRIMARY, "^KS11"]:
        try: return float(_provider_chain_get_change([sym], True, True)), sym
        except Exception as e: log.warning("%s 실패: %s", sym, e)
    raise RuntimeError("KR 데이터 수집 실패")

# -------------------- 레벨 판정 --------------------
def grade_level(delta_pct: float, is_vix: bool=False) -> str|None:
    a=abs(delta_pct)
    if is_vix:
        if a>=10: return "LV3"
        if a>=7: return "LV2"
        if a>=5: return "LV1"
    else:
        if a>=2.5: return "LV3"
        if a>=1.5: return "LV2"
        if a>=0.8: return "LV1"
    return None

# -------------------- 알림 전송 --------------------
def post_alert(delta_pct: float|None, level: str|None, source_tag: str, note: str):
    display_name = human_name(source_tag)
    payload = {
        "index": display_name,
        "level": level or "CLEARED",
        "delta_pct": round(delta_pct,2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": note,
    }
    headers={"Content-Type":"application/json"}
    if SENTINEL_KEY: headers["x-sentinel-key"]=SENTINEL_KEY
    url=f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r=requests.post(url,headers=headers,json=payload,timeout=15)
        if not r.ok: log.error("알림 전송 실패 %s %s",r.status_code,r.text)
        else: log.info("알림 전송: %s %s %.2f%% (%s)",display_name,level or "CLEARED",delta_pct or 0,note)
    except Exception as e: log.error("알림 전송 오류: %s",e)

# -------------------- 메인 감시 --------------------
def check_and_alert():
    state=_load_state(); sess=current_session()
    log.info("===== 시장 체크 [%s 세션] =====",sess); state["last_checked_at"]=_now_kst_iso()
    if sess=="KR":
        try:
            delta,tag=get_delta_k200(); lvl=grade_level(delta); prev=state.get("ΔK200")
            name=human_name(tag)
            log.info("%s: %.2f%% [%s → %s]",name,delta,prev or "없음",lvl or "정상")
            if lvl!=prev:
                note="레벨 진입" if(not prev and lvl) else "레벨 해제" if(prev and not lvl) else f"{prev} → {lvl}"
                post_alert(delta,lvl,tag,note); state["ΔK200"]=lvl
        except Exception as e: log.warning("KR 수집 실패: %s",e)
    else:
        us_open=is_us_market_open()
        symbols=[(SYMBOL_SPX_FUT,"ΔES"),(SYMBOL_NDX_FUT,"ΔNQ")] if not us_open else [(SYMBOL_SPX,"ΔSPX"),(SYMBOL_NDX,"ΔNASDAQ"),(SYMBOL_VIX,"ΔVIX")]
        for sym,key in symbols:
            try:
                delta=get_delta(sym); is_vix=(sym==SYMBOL_VIX); name=human_name(sym)
                lvl=grade_level(delta,is_vix); prev=state.get(key)
                log.info("%s: %.2f%% [%s → %s]",name,delta,prev or "없음",lvl or "정상")
                if lvl!=prev:
                    note="레벨 진입" if(not prev and lvl) else "레벨 해제" if(prev and not lvl) else f"{prev} → {lvl}"
                    if is_vix and lvl and us_open:
                        # S&P/NAS 값도 같이 붙이려면 여기서 다시 불러오면 됨
                        pass
                    post_alert(delta,lvl,sym,note); state[key]=lvl
            except Exception as e: log.warning("%s 수집 실패: %s",human_name(sym),e)
    _save_state(state); log.info("===== 체크 완료 =====")

# -------------------- 메인 루프 --------------------
def run_loop():
    log.info("=== Sentinel 시장감시 시작 ==="); log.info("간격: %d초",WATCH_INTERVAL)
    log.info("데이터 소스(우선순위): %s",", ".join(DATA_PROVIDERS))
    log.info("임계값 - 일반: 0.8%/1.5%/2.5%, VIX: 5%/7%/10%"); log.info("신선도 한도: %ds",MAX_STALENESS_SEC)
    try: check_and_alert()
    except Exception as e: log.error("초기 체크 실패: %s",e)
    while True:
        time.sleep(WATCH_INTERVAL)
        try: check_and_alert()
        except Exception as e: log.error("주기 체크 오류: %s",e)

if __name__=="__main__": run_loop()
