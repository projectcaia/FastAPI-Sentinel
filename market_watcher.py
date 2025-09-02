# market_watcher.py — FGPT Sentinel 시장감시 워커 (수정판)
# -*- coding: utf-8 -*-

import os, time, json, logging, requests, math
from datetime import datetime, timezone, timedelta

# ==================== 설정/로그 ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('market_watcher.log', encoding='utf-8')
    ]
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
VIX_FILTER_THRESHOLD = parse_float_env("VIX_FILTER_THRESHOLD", 0.8)
FORCE_ALERT_INTERVAL = parse_int_env("FORCE_ALERT_HOURS", 4)  # 4시간마다 강제 알림

STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# yfinance 설정
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
    "^KS200":     "KOSPI 200",
    "^KS11":      "KOSPI",
    "069500.KS":  "KODEX 200",
    "102110.KS":  "TIGER 200",
    "^GSPC":      "S&P 500",
    "^IXIC":      "NASDAQ",
    "^VIX":       "VIX",
    "ES=F":       "S&P 500 선물",
    "NQ=F":       "NASDAQ-100 선물",
}

def human_name(sym: str) -> str:
    return HUMAN_NAMES.get(sym, sym)

# 심볼 정의
KR_SPOT_PRIORITY = ["^KS11", "069500.KS", "102110.KS", "^KS200"]
US_SPOT = ["^GSPC", "^IXIC", "^VIX"]
FUTURES_SYMBOLS = ["ES=F", "NQ=F"]

# ==================== 시간 유틸 ====================
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def _utc_ts_now() -> float:
    return datetime.now(timezone.utc).timestamp()

# ==================== 상태 파일 (개선) ====================
def _save_state(state: dict):
    """상태 저장 - 안정성 강화"""
    try:
        # 임시 파일에 먼저 쓰고 원자적으로 교체
        temp_path = STATE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        
        # 원자적 교체
        if os.path.exists(temp_path):
            os.replace(temp_path, STATE_PATH)
            log.debug("상태 저장 완료: %s", STATE_PATH)
    except Exception as e:
        log.error("상태 저장 실패: %s", e)

def _load_state() -> dict:
    """상태 로드 - 안정성 강화"""
    if not os.path.exists(STATE_PATH):
        log.info("상태 파일 없음 - 새로 생성")
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
            log.debug("상태 로드 완료: %s", list(state.keys()))
            return state
    except Exception as e:
        log.error("상태 로드 실패: %s - 초기화", e)
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

# ==================== 데이터 수집 (당일 변화율) ====================
def get_intraday_change(symbol: str, is_kr: bool = False) -> tuple[float | None, str]:
    """당일 시가 대비 현재 변화율 계산"""
    
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
            
            # 당일 시가와 현재가
            current = q.get("regularMarketPrice")
            open_price = q.get("regularMarketOpen")
            
            if current and open_price and open_price != 0:
                change_pct = (float(current) - float(open_price)) / float(open_price) * 100.0
                log.info("[%s] %s 당일: 시가=%.2f, 현재=%.2f, 변화율=%.2f%%", 
                        "한국" if is_kr else "미국", symbol, float(open_price), float(current), change_pct)
                return change_pct, "yahoo"
                
    except Exception as e:
        log.debug("Yahoo API 실패(%s): %s", symbol, e)
    
    # Chart API 폴백
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "interval": "5m" if is_kr else "1m",
            "range": "1d"
        }
        
        r = _http_get(url, params=params)
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        indicators = chart.get("indicators", {}).get("quote", [{}])[0]
        
        current = meta.get("regularMarketPrice")
        opens = indicators.get("open", [])
        
        if current and opens:
            # 첫 번째 유효한 시가 찾기
            open_price = None
            for o in opens:
                if o is not None:
                    open_price = float(o)
                    break
            
            if open_price and open_price != 0:
                change_pct = (float(current) - open_price) / open_price * 100.0
                log.info("%s Chart 당일: 시가=%.2f, 현재=%.2f, 변화율=%.2f%%", 
                        symbol, open_price, float(current), change_pct)
                return change_pct, "chart"
            
    except Exception as e:
        log.debug("Chart API 실패(%s): %s", symbol, e)
    
    return None, "failed"

# ==================== 레벨 판정 ====================
def grade_level(delta_pct: float, is_vix: bool = False) -> str | None:
    a = abs(delta_pct)
    
    if is_vix:
        # VIX 레벨
        if a >= 25.0: return "LV3"
        if a >= 15.0: return "LV2"
        if a >= 8.0:  return "LV1"
    else:
        # 일반 지수
        if a >= 2.5: return "LV3"
        if a >= 1.5: return "LV2"
        if a >= 0.8: return "LV1"
    return None

# ==================== 알림 전송 ====================
def post_alert(delta_pct: float | None, level: str | None, source_tag: str, note: str, kind: str = "ALERT"):
    """알림 전송"""
    display_name = human_name(source_tag)
    payload = {
        "index": display_name,
        "level": level or "NORMAL",
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": note,
        "kind": kind,
        "symbol": source_tag,
    }
    
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY
    
    if not SENTINEL_BASE_URL:
        log.warning("SENTINEL_BASE_URL 미설정 - 알림 스킵")
        return
    
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if not r.ok:
            log.error("알림 전송 실패 %s %s", r.status_code, r.text)
        else:
            log.info(">>> 알림 전송 성공: [%s] %s %s %.2f%% (%s)", 
                    kind, display_name, level or "NORMAL", delta_pct or 0, note)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# ==================== 메인 감시 로직 (수정) ====================
def check_and_alert():
    """메인 감시 로직 - 상태 관리 개선"""
    state = _load_state()
    sess = current_session()
    
    log.info("="*60)
    log.info("시장 체크 시작 [세션: %s] %s", sess, _now_kst().strftime("%Y-%m-%d %H:%M:%S KST"))
    log.info("="*60)
    
    state["last_checked_at"] = _now_kst_iso()
    state["last_session"] = sess
    
    # 강제 알림 체크 (4시간마다)
    last_alert_time = state.get("last_alert_time", 0)
    current_time = time.time()
    force_alert = (current_time - last_alert_time) > (FORCE_ALERT_INTERVAL * 3600)
    
    if sess == "CLOSED":
        log.info("시장 휴장 중 - 감시 스킵")
        _save_state(state)
        return
    
    if sess == "KR":
        # 한국 정규장
        log.info("【한국 정규장】 당일 데이터 수집 중...")
        
        collected = False
        for sym in KR_SPOT_PRIORITY:
            delta, source = get_intraday_change(sym, is_kr=True)
            if delta is not None:
                lvl = grade_level(delta)
                
                # 상태 키 수정 (심볼별로 구분)
                state_key = f"KR_{sym}"
                prev_state = state.get(state_key, {})
                prev_lvl = prev_state.get("level")
                prev_delta = prev_state.get("delta")
                
                name = human_name(sym)
                
                log.info("✓ %s: 당일 변화율 %.2f%% [이전:%s → 현재:%s] (데이터:%s)", 
                        name, delta, prev_lvl or "없음", lvl or "정상", source)
                
                # 알림 조건: 레벨 변경 또는 강제 알림 또는 큰 변화
                should_alert = False
                alert_note = ""
                
                if lvl != prev_lvl:
                    # 레벨 변경
                    should_alert = True
                    if not prev_lvl and lvl:
                        alert_note = f"당일 {lvl} 레벨 진입"
                    elif prev_lvl and not lvl:
                        alert_note = "당일 레벨 해제 (정상 복귀)"
                    else:
                        alert_note = f"당일 레벨 변경: {prev_lvl} → {lvl}"
                
                elif force_alert and lvl:
                    # 강제 알림 (레벨이 있을 때만)
                    should_alert = True
                    alert_note = f"정기 알림: {lvl} 유지 중"
                
                elif prev_delta is not None and abs(delta - prev_delta) >= 0.5:
                    # 큰 변화 감지 (0.5% 이상 변화)
                    should_alert = True
                    alert_note = f"변화율 급변: {prev_delta:.2f}% → {delta:.2f}%"
                
                if should_alert:
                    post_alert(delta, lvl, sym, alert_note)
                    state["last_alert_time"] = current_time
                
                # 상태 업데이트
                state[state_key] = {
                    "level": lvl,
                    "delta": delta,
                    "updated_at": _now_kst_iso()
                }
                
                collected = True
                break
        
        if not collected:
            log.error("⚠ 한국 시장 데이터 수집 실패! 모든 심볼 시도 실패")
    
    elif sess == "US":
        # 미국 정규장
        log.info("【미국 정규장】 당일 데이터 수집 중...")
        
        # S&P, NASDAQ 먼저 수집
        spx_delta, _ = get_intraday_change("^GSPC")
        ndx_delta, _ = get_intraday_change("^IXIC")
        
        max_index_move = 0.0
        if spx_delta is not None and ndx_delta is not None:
            max_index_move = max(abs(spx_delta), abs(ndx_delta))
            log.info("당일 지수 변동: S&P %.2f%%, NASDAQ %.2f%%", spx_delta, ndx_delta)
        
        for sym in US_SPOT:
            delta, source = get_intraday_change(sym)
            if delta is None:
                log.warning("⚠ %s 데이터 수집 실패", human_name(sym))
                continue
            
            is_vix = (sym == "^VIX")
            
            # VIX 필터
            if is_vix and max_index_move < VIX_FILTER_THRESHOLD:
                log.info("VIX 필터 적용: 지수 변동 %.2f%% < %.2f%% → VIX %.2f%% 무시", 
                        max_index_move, VIX_FILTER_THRESHOLD, delta)
                continue
            
            lvl = grade_level(delta, is_vix=is_vix)
            
            # 상태 키
            state_key = f"US_{sym}"
            prev_state = state.get(state_key, {})
            prev_lvl = prev_state.get("level")
            prev_delta = prev_state.get("delta")
            
            name = human_name(sym)
            
            log.info("✓ %s: 당일 %.2f%% [이전:%s → 현재:%s]", 
                    name, delta, prev_lvl or "없음", lvl or "정상")
            
            # 알림 조건
            should_alert = False
            alert_note = ""
            
            if lvl != prev_lvl:
                should_alert = True
                if not prev_lvl and lvl:
                    alert_note = f"당일 {lvl} 레벨 진입"
                elif prev_lvl and not lvl:
                    alert_note = "당일 레벨 해제"
                else:
                    alert_note = f"당일 {prev_lvl} → {lvl}"
                    
                if is_vix and lvl and spx_delta and ndx_delta:
                    alert_note += f" (S&P {spx_delta:+.2f}%, NAS {ndx_delta:+.2f}%)"
            
            elif force_alert and lvl:
                should_alert = True
                alert_note = f"정기 알림: {lvl} 유지 중"
            
            elif prev_delta is not None and abs(delta - prev_delta) >= 0.5:
                should_alert = True
                alert_note = f"변화율 급변: {prev_delta:.2f}% → {delta:.2f}%"
            
            if should_alert:
                post_alert(delta, lvl, sym, alert_note)
                state["last_alert_time"] = current_time
            
            # 상태 업데이트
            state[state_key] = {
                "level": lvl,
                "delta": delta,
                "updated_at": _now_kst_iso()
            }
    
    elif sess == "FUTURES":
        # 선물 시간
        log.info("【선물 시장】 세션 데이터 수집 중...")
        
        for sym in FUTURES_SYMBOLS:
            delta, source = get_intraday_change(sym)
            name = human_name(sym)
            
            if delta is None:
                log.info("⚠ %s: 데이터 없음", name)
                continue
            
            log.info("✓ %s: 세션 변화율 %.2f%% (데이터:%s)", name, delta, source)
            
            # 상태 키
            state_key = f"FUT_{sym}"
            prev_state = state.get(state_key, {})
            prev_delta = prev_state.get("delta")
            
            # 0.8% 이상 변화시 알림
            if abs(delta) >= 0.8:
                # 노이즈 억제
                if prev_delta is None or abs(delta - prev_delta) >= 0.2:
                    note = f"선물 세션 변동 {delta:+.2f}%"
                    post_alert(delta, "PRE", sym, note, kind="PRE")
                    state["last_alert_time"] = current_time
            
            # 상태 업데이트
            state[state_key] = {
                "delta": delta,
                "updated_at": _now_kst_iso()
            }
    
    _save_state(state)
    log.info("체크 완료 - 상태 저장됨")
    log.info("-"*60)

# ==================== 시장 시간 판정 ====================
def current_session() -> str:
    """현재 세션 판정"""
    now = _now_kst()
    
    # 주말
    if now.weekday() >= 5:
        return "CLOSED"
    
    hhmm = now.hour * 100 + now.minute
    
    # 한국 정규장: 09:00 ~ 15:30
    if 900 <= hhmm <= 1530:
        return "KR"
    
    # 미국 정규장 (서머타임): 22:30 ~ 05:00
    # 미국 정규장 (표준시): 23:30 ~ 06:00
    month = now.month
    is_dst = 3 <= month <= 11
    
    if is_dst:
        if (hhmm >= 2230) or (hhmm < 500):
            return "US"
    else:
        if (hhmm >= 2330) or (hhmm < 600):
            return "US"
    
    # 선물 시간
    if 1530 < hhmm < 2230:
        return "FUTURES"
    
    return "CLOSED"

# ==================== 메인 루프 ====================
def run_loop():
    log.info("="*60)
    log.info("Sentinel 시장감시 시작 (당일 변화율 모드)")
    log.info("="*60)
    log.info("설정:")
    log.info("  - 체크 간격: %d초", WATCH_INTERVAL)
    log.info("  - 일반 임계값: 0.8% / 1.5% / 2.5%")
    log.info("  - VIX 임계값: 8% / 15% / 25%")
    log.info("  - VIX 필터: 지수 변동 %.1f%% 미만시 무시", VIX_FILTER_THRESHOLD)
    log.info("  - 강제 알림: %d시간마다", FORCE_ALERT_INTERVAL)
    log.info("  - 상태 파일: %s", STATE_PATH)
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
