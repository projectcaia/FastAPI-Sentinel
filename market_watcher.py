# market_watcher.py — FGPT Sentinel 시장감시 워커 (정규장 실시간 보정 버전)
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
K_SIGMA = parse_float_env("BOLL_K_SIGMA", 2.5)
BB_WINDOW = parse_int_env("BOLL_WINDOW", 20)

# 멀티소스 설정
YF_ENABLED = os.getenv("YF_ENABLED", "false").lower() in ("1", "true", "yes")
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS", "yahoo,yfinance").split(",") if s.strip()]
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()

# Alpha Vantage ETF 프록시
AV_PROXY_MAP = {
    "^GSPC": "SPY",
    "^IXIC": "QQQ",
    "^VIX": "VIXY",
    "^KS200": "069500.KS"
}

# 공통 헤더
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
}

# 심볼 정의
SYMBOL_PRIMARY = "^KS200"
SYMBOL_FALLBACKS = ["069500.KS", "102110.KS", "^KS11"]
SYMBOL_SPX = "^GSPC"
SYMBOL_NDX = "^IXIC"
SYMBOL_VIX = "^VIX"
SYMBOL_SPX_FUT = "ES=F"
SYMBOL_NDX_FUT = "NQ=F"

# 표시 이름
DISPLAY_NAMES = {
    "^KS200": "한국 시장: KOSPI200",
    "069500.KS": "한국 시장: KODEX200",
    "102110.KS": "한국 시장: TIGER200",
    "^KS11": "한국 시장: 코스피",
    "^GSPC": "미국 S&P500",
    "^IXIC": "미국 NASDAQ",
    "^VIX": "미국 VIX: 변동성지수",
    "ES=F": "미국 S&P500 선물",
    "NQ=F": "미국 NASDAQ 선물"
}

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

# -------------------- HTTP 유틸 --------------------
def _http_get(url: str, params=None, timeout=10, max_retry=3):
    last = None
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
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
    raise RuntimeError("HTTP 요청 실패")

# -------------------- Yahoo Finance --------------------
def _yahoo_quote(symbols):
    symbols_param = ",".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    # 특정 필드를 강제하지 않아도 되지만, 가격/전일종가/마켓상태/업데이트시간은 꼭 필요
    r = _http_get(url, params={"symbols": symbols_param}, timeout=12, max_retry=3)
    return r.json()

def _epoch_to_kst_iso(ts: int | float | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    except Exception:
        return None

def _pick_price_fields(q: dict) -> tuple[float | None, float | None, str | None, str | None]:
    """
    실시간 장중 데이터(regularMarketPrice)를 우선 사용.
    보조로 regularMarketPreviousClose. marketState/regularMarketTime 로그 보강.
    """
    price = q.get("regularMarketPrice") or q.get("price")
    prev = q.get("regularMarketPreviousClose") or q.get("previousClose")
    market_state = q.get("marketState")  # "REGULAR", "CLOSED", "PRE", "POST" 등
    updated_at = _epoch_to_kst_iso(q.get("regularMarketTime") or q.get("postMarketTime") or q.get("preMarketTime"))
    try:
        price = float(price) if price is not None else None
    except Exception:
        price = None
    try:
        prev = float(prev) if prev is not None else None
    except Exception:
        prev = None
    return price, prev, market_state, updated_at

def _extract_change_percent(q: dict, symbol: str = None, require_regular: bool | None = None):
    """
    정규장 실시간 데이터 우선, 불가 시 종가 대비 계산.
    require_regular=True 이면 REGULAR 상태가 아니면 None 반환하여 폴백 트리거.
    """
    price, prev, market_state, updated_at = _pick_price_fields(q)

    # 한국 시장은 장중 'REGULAR' 확인을 더 엄격히 적용(전일 종가 고정 문제 방지)
    is_kr = symbol and (".KS" in symbol or "^KS" in symbol)
    if require_regular is None:
        require_regular = bool(is_kr)

    if require_regular and market_state and market_state != "REGULAR":
        log.debug("마켓상태 REGULAR 아님(symbol=%s, state=%s, updated=%s)", symbol, market_state, updated_at)
        return None  # 폴백 시도 유도

    # 정상 계산 경로
    if price is not None and prev not in (None, 0.0):
        try:
            return (price - prev) / prev * 100.0
        except Exception:
            pass

    # 마지막 시도: API 변화율 직접 제공값
    cp = q.get("regularMarketChangePercent")
    if cp is not None:
        try:
            return float(cp)
        except Exception:
            return None

    return None

# -------------------- yfinance --------------------
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance import 실패: %s", e)

def _yf_change_percent(symbol: str):
    if not _YF_READY:
        raise RuntimeError("yfinance not ready")

    t = yf.Ticker(symbol)

    # fast_info 시도 (실시간에 더 가깝다)
    info = getattr(t, "fast_info", None)
    if info:
        last = getattr(info, "regularMarketPrice", None) or getattr(info, "last_price", None)
        prev = getattr(info, "regularMarketPreviousClose", None) or getattr(info, "previous_close", None)
        try:
            if last is not None and prev not in (None, 0):
                return (float(last) - float(prev)) / float(prev) * 100.0
        except Exception:
            pass

    # history 폴백(일봉: 실시간 아님, 마지막 종가 기준)
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

    if "Note" in data or "Information" in data:
        raise RuntimeError(f"AV rate limit: {data}")

    q = data.get("Global Quote", {})
    if not q:
        raise RuntimeError(f"AV empty for {sym}")

    cp = q.get("10. change percent")
    if cp:
        try:
            return float(str(cp).strip().rstrip("%"))
        except Exception:
            pass

    price = q.get("05. price")
    prev = q.get("08. previous close")
    if price and prev and float(prev) != 0:
        return (float(price) - float(prev)) / float(prev) * 100.0

    raise RuntimeError(f"AV invalid quote for {sym}")

# -------------------- 프로바이더 체인 --------------------
def _provider_chain_get_change(symbols, require_regular_by_symbol: bool = False):
    """
    멀티소스 체인으로 변화율 수집.
    require_regular_by_symbol=True면 야후에서 REGULAR 상태 아니면 폴백 유도.
    """
    last_err = None

    if not isinstance(symbols, (list, tuple)):
        symbols = [symbols]

    for provider in DATA_PROVIDERS:
        try:
            if provider == "yahoo":
                data = _yahoo_quote(symbols)
                items = data.get("quoteResponse", {}).get("result", [])
                if not items:
                    raise RuntimeError("yahoo empty response")

                vals = []
                for idx, sym in enumerate(symbols):
                    if idx < len(items):
                        cp = _extract_change_percent(
                            items[idx],
                            sym,
                            require_regular=require_regular_by_symbol
                        )
                        if cp is not None and not math.isnan(cp):
                            vals.append(float(cp))

                if vals:
                    return sum(vals) / len(vals)
                raise RuntimeError("yahoo no valid data")

            elif provider == "yfinance" and _YF_READY:
                vals = []
                for sym in symbols:
                    try:
                        vals.append(_yf_change_percent(sym))
                    except Exception:
                        continue
                if vals:
                    return sum(vals) / len(vals)
                raise RuntimeError("yfinance no valid data")

            elif provider == "alphavantage" and ALPHAVANTAGE_API_KEY:
                vals = []
                for sym in symbols:
                    try:
                        vals.append(_alphavantage_change_percent(sym))
                    except Exception:
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

# -------------------- 변화율 수집 --------------------
def get_delta(symbol) -> float:
    # 미국 선물/현물은 REGULAR 강제 X (장외도 활용)
    require_regular = False
    # 한국 심볼은 REGULAR 강제(장중 전일종가 고정 문제 방지)
    if ".KS" in symbol or "^KS" in symbol:
        require_regular = True
    return float(_provider_chain_get_change([symbol], require_regular_by_symbol=require_regular))

def get_delta_k200() -> tuple[float, str]:
    """
    한국 시장 변화율 (정규장 실시간 ETF 우선)
    - 1순위: 069500.KS (KODEX200) — 실시간 반영 안정적
    - 2순위: ^KS200
    - 3순위: ^KS11 (코스피)
    """
    # 1) ETF (KODEX200) 실시간
    try:
        cp = _provider_chain_get_change(["069500.KS"], require_regular_by_symbol=True)
        return float(cp), "069500.KS"
    except Exception as e:
        log.warning("ETF(KODEX200) 실패: %s", e)

    # 2) KOSPI200 지수
    try:
        cp = _provider_chain_get_change([SYMBOL_PRIMARY], require_regular_by_symbol=True)
        return float(cp), SYMBOL_PRIMARY
    except Exception as e:
        log.warning("^KS200 실패: %s", e)

    # 3) KOSPI 지수
    try:
        cp = _provider_chain_get_change([SYMBOL_FALLBACKS[-1]], require_regular_by_symbol=True)
        return float(cp), SYMBOL_FALLBACKS[-1]
    except Exception as e:
        log.warning("KOSPI 실패: %s", e)

    raise RuntimeError("KR 데이터 수집 실패")

# -------------------- 레벨 판정 --------------------
def grade_level(delta_pct: float, is_vix: bool = False) -> str | None:
    """변화율에 따른 레벨 판정"""
    a = abs(delta_pct)

    if is_vix:
        if a >= 10.0: return "LV3"
        if a >= 7.0: return "LV2"
        if a >= 5.0: return "LV1"
    else:
        if a >= 2.5: return "LV3"
        if a >= 1.5: return "LV2"
        if a >= 0.8: return "LV1"
    return None

# -------------------- 알림 전송 --------------------
def post_alert(index_name: str, delta_pct: float | None, level: str | None, source_tag: str, note: str):
    """센티넬로 알림 전송"""
    display_name = DISPLAY_NAMES.get(source_tag, index_name)

    payload = {
        "index": display_name,
        "level": level or "CLEARED",
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": note,
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
            log.info("알림 전송: %s %s %.2f%% (%s)", display_name, level or "CLEARED", delta_pct or 0, note)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# -------------------- 세션 판별 --------------------
def current_session() -> str:
    """KST 기준 세션 판별"""
    now = _now_kst()
    hhmm = now.hour * 100 + now.minute
    if now.weekday() >= 5:  # 주말
        return "US"
    return "KR" if 830 <= hhmm <= 1600 else "US"

def is_us_market_open() -> bool:
    """미국 정규장 시간 체크 (KST 기준, 서머타임 단순화)"""
    now = _now_kst()
    hour = now.hour
    minute = now.minute
    month = now.month
    # 대략적 DST 범위 (3~11월)
    is_dst = 3 <= month <= 11

    if is_dst:  # 서머타임: 22:30 ~ 05:00
        if hour == 22 and minute >= 30:
            return True
        elif hour >= 23 or hour < 5:
            return True
    else:  # 표준시: 23:30 ~ 06:00
        if hour == 23 and minute >= 30:
            return True
        elif 0 <= hour < 6:
            return True

    return False

# -------------------- 메인 감시 --------------------
def check_and_alert():
    state = _load_state()
    sess = current_session()

    log.info("===== 시장 체크 [%s 세션] =====", sess)

    # 최근 수집시각 기록 보강
    state["last_checked_at"] = _now_kst_iso()

    if sess == "KR":
        try:
            delta, tag = get_delta_k200()
            lvl = grade_level(delta)
            prev = state.get("ΔK200")

            log.info("KR: %.2f%% [%s → %s]", delta, prev or "없음", lvl or "정상")

            if lvl != prev:
                if not prev and lvl:
                    note = "레벨 진입"
                elif prev and not lvl:
                    note = "레벨 해제"
                else:
                    note = f"{prev} → {lvl}"

                post_alert("ΔK200", delta, lvl, tag, note)
                state["ΔK200"] = lvl

        except Exception as e:
            log.warning("KR 수집 실패: %s", e)
    else:
        # 미국 세션
        spx_delta = 0.0
        nasdaq_delta = 0.0

        # 지수 데이터 먼저 수집 (VIX 필터용)
        try:
            spx_delta = get_delta(SYMBOL_SPX)
        except Exception as e:
            log.debug("SPX 선행 수집 실패: %s", e)

        try:
            nasdaq_delta = get_delta(SYMBOL_NDX)
        except Exception as e:
            log.debug("NASDAQ 선행 수집 실패: %s", e)

        # 시장 상태 확인
        us_market_open = is_us_market_open()

        if not us_market_open:
            log.info("미국 시장 마감 - 선물 지수 사용")
            symbols = [
                ("ΔES", SYMBOL_SPX_FUT, "ES=F"),
                ("ΔNQ", SYMBOL_NDX_FUT, "NQ=F"),
            ]
        else:
            log.info("미국 시장 개장 - 현물 지수 사용")
            symbols = [
                ("ΔSPX", SYMBOL_SPX, SYMBOL_SPX),
                ("ΔNASDAQ", SYMBOL_NDX, SYMBOL_NDX),
                ("ΔVIX", SYMBOL_VIX, SYMBOL_VIX)
            ]

        for idx_name, sym, tag in symbols:
            try:
                delta = get_delta(sym)
                is_vix = (sym == SYMBOL_VIX)

                # VIX 스마트 필터
                if is_vix:
                    max_index_move = max(abs(spx_delta), abs(nasdaq_delta))
                    if max_index_move < 0.8:
                        log.debug("VIX 필터: 지수 %.2f%% 대비 VIX %.2f%% - 무시", max_index_move, delta)
                        state[idx_name] = None
                        continue

                lvl = grade_level(delta, is_vix=is_vix)
                prev = state.get(idx_name)

                log.info("%s: %.2f%% [%s → %s]", idx_name, delta, prev or "없음", lvl or "정상")

                if lvl != prev:
                    if not prev and lvl:
                        note = "레벨 진입"
                    elif prev and not lvl:
                        note = "레벨 해제"
                    else:
                        note = f"{prev} → {lvl}"

                    if is_vix and lvl:
                        note += f" (S&P {spx_delta:+.2f}%, NAS {nasdaq_delta:+.2f}%)"

                    post_alert(idx_name, delta, lvl, tag, note)
                    state[idx_name] = lvl

            except Exception as e:
                log.warning("%s 수집 실패: %s", idx_name, e)

    _save_state(state)
    log.info("===== 체크 완료 =====")

# -------------------- 메인 루프 --------------------
def run_loop():
    log.info("=== Sentinel 시장감시 시작 ===")
    log.info("간격: %d초", WATCH_INTERVAL)
    log.info("데이터 소스: %s", ", ".join(DATA_PROVIDERS))
    log.info("임계값 - 일반: 0.8%/1.5%/2.5%, VIX: 5%/7%/10%")

    # 초기 체크
    try:
        check_and_alert()
    except Exception as e:
        log.error("초기 체크 실패: %s", e)

    # 주기 루프
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            check_and_alert()
        except Exception as e:
            log.error("주기 체크 오류: %s", e)

if __name__ == "__main__":
    run_loop()
