# market_watcher.py — FGPT Sentinel 시장감시 워커 (선물 중심 버전)
# -*- coding: utf-8 -*-

import os, time, json, logging, requests, math
from datetime import datetime, timezone, timedelta

# ==================== 설정/로그 ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
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

STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# yfinance 설정 - 경고 억제
import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

YF_ENABLED = os.getenv("YF_ENABLED", "true").lower() in ("1", "true", "yes")
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf
        import pandas as pd
        _YF_READY = True
        log.info("yfinance 모듈 준비 완료")
    except Exception as e:
        log.warning("yfinance import 실패: %s", e)

# ==================== 심볼/표기 ====================
HUMAN_NAMES = {
    # 한국 현물
    "^KS11":      "KOSPI",
    "^KS200":     "KOSPI 200",
    "069500.KS":  "KODEX 200",
    "102110.KS":  "TIGER 200",
    # 미국 선물
    "ES=F":       "S&P 500 선물",
    "NQ=F":       "NASDAQ 선물",
}

def human_name(sym: str) -> str:
    return HUMAN_NAMES.get(sym, sym)

# 심볼 정의
KR_SPOT = ["^KS11", "069500.KS", "102110.KS", "^KS200"]  # KOSPI 우선
US_FUTURES = ["ES=F", "NQ=F"]  # 선물만

# ==================== 시간 유틸 ====================
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def _utc_ts_now() -> float:
    return datetime.now(timezone.utc).timestamp()

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
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
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

# ==================== 데이터 수집 ====================
def get_kr_intraday(symbol: str) -> tuple[float | None, str]:
    """한국 현물 당일 변화율"""
    
    # Yahoo Quote API 우선
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {
            "symbols": symbol,
            "crumb": str(int(time.time()))
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            q = items[0]
            
            current = q.get("regularMarketPrice")
            open_price = q.get("regularMarketOpen")
            
            if current and open_price and open_price != 0:
                change_pct = (float(current) - float(open_price)) / float(open_price) * 100.0
                log.info("[한국] %s: 시가=%d, 현재=%d, 변화율=%.2f%%", 
                        symbol, int(open_price), int(current), change_pct)
                return change_pct, "yahoo"
                    
    except Exception as e:
        log.debug("Yahoo API 실패(%s): %s", symbol, e)
    
    # yfinance 폴백
    if _YF_READY:
        try:
            ticker = yf.download(
                symbol, 
                period="1d", 
                interval="5m",
                progress=False, 
                auto_adjust=True,
                threads=False
            )
            
            if ticker is not None and len(ticker) > 0:
                open_price = ticker["Open"].iloc[0] if len(ticker) > 0 else None
                current = ticker["Close"].iloc[-1] if len(ticker) > 0 else None
                
                if open_price and current and open_price != 0:
                    open_price = float(open_price)
                    current = float(current)
                    change_pct = (current - open_price) / open_price * 100.0
                    log.info("[한국] %s: 시가=%d, 현재=%d, 변화율=%.2f%%", 
                            symbol, int(open_price), int(current), change_pct)
                    return change_pct, "yfinance"
        except Exception as e:
            log.debug("yfinance 실패(%s): %s", symbol, e)
    
    return None, "failed"

def get_futures_data(symbol: str) -> tuple[float | None, str]:
    """미국 선물 데이터 - 세션 기준"""
    
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "interval": "15m",
            "range": "1d"
        }
        
        r = _http_get(url, params=params)
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        indicators = chart.get("indicators", {}).get("quote", [{}])[0]
        
        current = meta.get("regularMarketPrice")
        opens = indicators.get("open", [])
        
        # 세션 시작 시가 찾기
        if current and opens:
            open_price = None
            for o in opens:
                if o is not None:
                    open_price = float(o)
                    break
            
            if open_price and open_price != 0:
                change_pct = (float(current) - open_price) / open_price * 100.0
                log.info("[선물] %s: 시가=%.2f, 현재=%.2f, 변화율=%.2f%%", 
                        symbol, open_price, float(current), change_pct)
                return change_pct, "chart"
                
    except Exception as e:
        log.debug("선물 데이터 실패(%s): %s", symbol, e)
    
    return None, "failed"

# ==================== 시장 시간 판정 ====================
def current_session() -> str:
    """현재 세션 판정 - 단순화"""
    now = _now_kst()
    
    # 주말
    if now.weekday() >= 5:
        # 일요일 저녁부터 선물 시작
        if now.weekday() == 6 and now.hour >= 18:  # 일요일 18시 이후
            return "FUTURES"
        return "CLOSED"
    
    hhmm = now.hour * 100 + now.minute
    
    # 한국 정규장: 09:00 ~ 15:30
    if 900 <= hhmm <= 1530:
        return "KR"
    
    # 그 외 시간은 모두 선물
    return "FUTURES"

# ==================== 레벨 판정 ====================
def grade_level(delta_pct: float) -> str | None:
    a = abs(delta_pct)
    
    if a >= 2.5: return "LV3"
    if a >= 1.5: return "LV2"
    if a >= 0.8: return "LV1"
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
            log.info(">>> 알림 전송: [%s] %s %s %.2f%% (%s)", 
                    kind, display_name, level or "CLEARED", delta_pct or 0, note)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# ==================== 메인 감시 로직 ====================
def check_and_alert():
    state = _load_state()
    sess = current_session()
    
    log.info("="*60)
    log.info("시장 체크 [%s] %s", sess, _now_kst().strftime("%Y-%m-%d %H:%M:%S KST"))
    log.info("="*60)
    
    state["last_checked_at"] = _now_kst_iso()
    
    if sess == "CLOSED":
        log.info("시장 휴장 - 감시 스킵")
        _save_state(state)
        return
    
    if sess == "KR":
        # 한국 정규장
        log.info("【한국 정규장】 당일 데이터 수집...")
        
        collected = False
        for sym in KR_SPOT:
            delta, source = get_kr_intraday(sym)
            if delta is not None:
                lvl = grade_level(delta)
                prev = state.get("ΔKOSPI")
                name = human_name(sym)
                
                log.info("✓ %s: %.2f%% [%s → %s] (소스: %s)", 
                        name, delta, prev or "없음", lvl or "정상", source)
                
                if lvl != prev:
                    note = ("레벨 진입" if (not prev and lvl) else
                           "레벨 해제" if (prev and not lvl) else
                           f"{prev} → {lvl}")
                    post_alert(delta, lvl, sym, note)
                    state["ΔKOSPI"] = lvl
                
                collected = True
                break
        
        if not collected:
            log.error("⚠ 한국 시장 데이터 수집 실패!")
    
    elif sess == "FUTURES":
        # 미국 선물
        log.info("【미국 선물】 세션 데이터 수집...")
        
        for sym in US_FUTURES:
            delta, source = get_futures_data(sym)
            name = human_name(sym)
            
            if delta is None:
                log.warning("⚠ %s: 데이터 없음", name)
                continue
            
            lvl = grade_level(delta)
            key = "ΔES" if sym == "ES=F" else "ΔNQ"
            prev = state.get(key)
            
            log.info("✓ %s: %.2f%% [%s → %s]", 
                    name, delta, prev or "없음", lvl or "정상")
            
            # 레벨 변경시 알림
            if lvl != prev:
                note = ("레벨 진입" if (not prev and lvl) else
                       "레벨 해제" if (prev and not lvl) else
                       f"{prev} → {lvl}")
                post_alert(delta, lvl, sym, note)
                state[key] = lvl
            
            # 선물 프리마켓 알림 (0.5% 이상 변동시)
            elif abs(delta) >= 0.5:
                prev_val = state.get(f"{key}_val", 0)
                if abs(delta - prev_val) >= 0.2:  # 0.2% 이상 추가 변동시
                    post_alert(delta, "PRE", sym, "선물 변동", kind="PRE")
                    state[f"{key}_val"] = delta
    
    _save_state(state)
    log.info("체크 완료")
    log.info("-"*60)

# ==================== 메인 루프 ====================
def run_loop():
    log.info("="*60)
    log.info("Sentinel 시장감시 시작 (선물 중심 모드)")
    log.info("="*60)
    log.info("설정:")
    log.info("  - 체크 간격: %d초", WATCH_INTERVAL)
    log.info("  - 레벨 임계값: 0.8% / 1.5% / 2.5%")
    log.info("  - 한국: KOSPI 현물만")
    log.info("  - 미국: ES/NQ 선물만")
    log.info("-"*60)
    
    # 초기 체크
    try:
        check_and_alert()
    except Exception as e:
        log.error("초기 체크 실패: %s", e, exc_info=True)
    
    # 주기적 체크
    while True:
        time.sleep(WATCH_INTERVAL)
        try:
            check_and_alert()
        except Exception as e:
            log.error("주기 체크 오류: %s", e, exc_info=True)

if __name__ == "__main__":
    run_loop()
