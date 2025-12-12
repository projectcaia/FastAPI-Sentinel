# market_watcher.py — FGPT Sentinel 시장감시 워커 (실시간 변동 감지)
# -*- coding: utf-8 -*-

import os, time, json, logging, requests, math, asyncio
from datetime import datetime, timezone, timedelta
from collections import deque
from zoneinfo import ZoneInfo

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

# 주기/설정
WATCH_INTERVAL = parse_int_env("WATCH_INTERVAL_SEC", 300)  # 5분
VIX_FILTER_THRESHOLD = parse_float_env("VIX_FILTER_THRESHOLD", 0.8)
FORCE_ALERT_INTERVAL = parse_int_env("FORCE_ALERT_HOURS", 4)
VOLATILITY_WINDOW = parse_int_env("VOLATILITY_WINDOW_MIN", 60)  # 60분 윈도우

STATE_PATH = os.getenv("WATCHER_STATE_PATH", "./market_state.json")

# ==================== 심볼/표기 ====================
HUMAN_NAMES = {
    "^KS200":     "KOSPI 200",
    "^KS11":      "KOSPI",
    "069500.KS":  "KODEX 200",
    "102110.KS":  "TIGER 200",
    "^KQ11F=F":   "K200 선물",
    "^KS200F=F":  "KOSPI200 선물",
    "^GSPC":      "S&P 500",
    "^IXIC":      "NASDAQ",
    "^VIX":       "VIX",
    "ES=F":       "S&P 500 선물",
    "NQ=F":       "NASDAQ-100 선물",
}

def human_name(sym: str) -> str:
    return HUMAN_NAMES.get(sym, sym)

# 심볼 정의
# 한국 시장: 주요 지수만 감시 (ETF는 보조용으로만 사용)
KR_MAIN_INDEX = "^KS11"  # KOSPI 메인
KR_ETF_BACKUP = ["069500.KS", "102110.KS"]  # KODEX, TIGER - KOSPI 데이터 없을 때만
KR_SPOT_PRIORITY = ["^KS11", "069500.KS", "102110.KS", "^KS200"]  # 호환성 유지
US_SPOT = ["^GSPC", "^IXIC", "^VIX"]
FUTURES_SYMBOLS = ["ES=F", "NQ=F"]

# DB증권 K200 선물 설정
K200_FUTURES_ENABLED = os.getenv("DBSEC_ENABLE", "true").lower() in ["true", "1", "yes"]
K200_FUTURES_CODE = os.getenv("DB_FUTURES_CODE", "101C6000").strip()
K200_CHECK_INTERVAL = parse_int_env("K200_CHECK_INTERVAL_MIN", 30)  # 30분 기본값

# ==================== 시간 유틸 ====================
KST = ZoneInfo("Asia/Seoul")
NYC = ZoneInfo("America/New_York")

def _now_kst():
    return datetime.now(KST)

def _now_nyc():
    return datetime.now(NYC)

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

def _utc_ts_now() -> float:
    return datetime.now(timezone.utc).timestamp()

# ==================== 상태 파일 ====================
def _save_state(state: dict):
    """상태 저장"""
    try:
        temp_path = STATE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        
        if os.path.exists(temp_path):
            os.replace(temp_path, STATE_PATH)
            log.debug("상태 저장 완료")
    except Exception as e:
        log.error("상태 저장 실패: %s", e)

def _load_state() -> dict:
    """상태 로드"""
    if not os.path.exists(STATE_PATH):
        log.info("상태 파일 없음 - 새로 생성")
        return {"price_history": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
            # price_history가 없으면 추가
            if "price_history" not in state:
                state["price_history"] = {}
            return state
    except Exception as e:
        log.error("상태 로드 실패: %s - 초기화", e)
        return {"price_history": {}}

# ==================== HTTP 유틸 ====================
H_COMMON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def _http_get(url: str, params=None, timeout=10, max_retry=2):
    last = None
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=H_COMMON, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            time.sleep(0.5 + i)
            last = e
            continue
    if last:
        raise last
    raise RuntimeError("HTTP 요청 실패")

# ==================== DB증권 K200 선물 데이터 수집 ====================
def get_k200_futures_data() -> dict | None:
    """DB증권 API를 통한 K200 선물 데이터 조회"""
    if not K200_FUTURES_ENABLED:
        return None
        
    try:
        # DB증권 API 설정
        api_base = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443").strip()
        app_key = os.getenv("DB_APP_KEY", "").strip()
        app_secret = os.getenv("DB_APP_SECRET", "").strip()
        
        if not app_key or not app_secret:
            log.warning("DB증권 API 키 미설정")
            return None
        
        # 토큰 발급 (간단 버전)
        token_url = f"{api_base}/oauth2/token"
        token_params = {
            "appkey": app_key,
            "appsecretkey": app_secret,
            "grant_type": "client_credentials",
            "scope": "oob"
        }
        
        token_resp = requests.post(
            token_url,
            params=token_params,
            headers={"Accept": "application/json"},
            timeout=10
        )
        
        if token_resp.status_code != 200:
            log.error("DB증권 토큰 발급 실패: %s - %s", token_resp.status_code, token_resp.text[:200])
            return None
            
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        
        if not access_token:
            log.error("DB증권 토큰 없음")
            return None
        
        # K200 선물 가격 조회
        quote_url = f"{api_base}/dfutureoption/quotations/v1/inquire-price"
        tr_id = "HHDFS76240000"
        
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {access_token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "custtype": "P",
            "tr_id": tr_id
        }
        
        payload = {
            "fid_cond_mrkt_div_code": "F",
            "fid_input_iscd": K200_FUTURES_CODE,
            "fid_input_iscd_cd": "1"
        }
        
        resp = requests.post(
            quote_url,
            headers=headers,
            json=payload,
            timeout=10,
            verify=False
        )
        
        if resp.status_code != 200:
            log.error("K200 선물 가격 조회 실패: %s - 종목코드:%s - %s", 
                     resp.status_code, K200_FUTURES_CODE, resp.text[:200])
            return None
        
        data = resp.json()
        
        # 응답 파싱
        for key in data.keys():
            if key.lower().startswith("output"):
                output = data[key]
                if not isinstance(output, dict):
                    continue
                
                # 현재가
                current = 0
                for field in ["futs_prpr", "stck_prpr", "prpr"]:
                    if field in output and output[field]:
                        try:
                            current = float(output[field])
                            if current > 0:
                                break
                        except:
                            continue
                
                # 시가
                open_price = 0
                for field in ["futs_oprc", "stck_oprc", "oprc"]:
                    if field in output and output[field]:
                        try:
                            open_price = float(output[field])
                            if open_price > 0:
                                break
                        except:
                            continue
                
                if current > 0 and open_price > 0:
                    change_pct = ((current - open_price) / open_price) * 100
                    
                    return {
                        "current": current,
                        "open": open_price,
                        "change_pct": change_pct,
                        "timestamp": time.time()
                    }
        
        log.error("K200 선물 데이터 파싱 실패")
        return None
        
    except Exception as e:
        log.error("K200 선물 조회 오류: %s", e)
        return None

# ==================== 데이터 수집 (개선) ====================
def get_market_data(symbol: str) -> dict | None:
    """시장 데이터 수집 - 현재가, 시가, 전일종가, 일중 고저"""
    try:
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {
            "symbols": symbol,
            "fields": "regularMarketPrice,regularMarketOpen,regularMarketPreviousClose,regularMarketDayHigh,regularMarketDayLow,regularMarketChangePercent,regularMarketTime",
            "crumb": str(int(time.time()))
        }
        r = _http_get(url, params=params)
        data = r.json()
        
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            q = items[0]
            market_time = q.get("regularMarketTime", 0)
            
            # 데이터 신선도 체크 (6시간 이상 오래된 데이터는 무시)
            now_ts = time.time()
            if market_time > 0 and (now_ts - market_time) > (6 * 3600):
                log.warning("%s: 오래된 데이터 감지 (%.1f시간 전) - 무시", 
                           symbol, (now_ts - market_time) / 3600)
                return None
            
            return {
                "current": q.get("regularMarketPrice"),
                "open": q.get("regularMarketOpen"),
                "prev_close": q.get("regularMarketPreviousClose"),
                "high": q.get("regularMarketDayHigh"),
                "low": q.get("regularMarketDayLow"),
                "change_pct": q.get("regularMarketChangePercent"),
                "timestamp": market_time or time.time()
            }
    except Exception as e:
        log.debug("Quote API 실패(%s): %s", symbol, e)
    
    # Chart API 폴백
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": "1m", "range": "1d"}
        
        r = _http_get(url, params=params)
        data = r.json()
        
        chart = data.get("chart", {}).get("result", [{}])[0]
        meta = chart.get("meta", {})
        indicators = chart.get("indicators", {}).get("quote", [{}])[0]
        
        current = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        
        # 시가, 고가, 저가 계산
        opens = [o for o in (indicators.get("open") or []) if o is not None]
        highs = [h for h in (indicators.get("high") or []) if h is not None]
        lows = [l for l in (indicators.get("low") or []) if l is not None]
        
        open_price = opens[0] if opens else None
        high = max(highs) if highs else None
        low = min(lows) if lows else None
        
        if current and prev_close:
            change_pct = ((current - prev_close) / prev_close) * 100
            
            return {
                "current": current,
                "open": open_price,
                "prev_close": prev_close,
                "high": high,
                "low": low,
                "change_pct": change_pct,
                "timestamp": time.time()
            }
    except Exception as e:
        log.debug("Chart API 실패(%s): %s", symbol, e)
    
    return None

# ==================== 변동성 계산 ====================
def calculate_volatility(state: dict, symbol: str, current_price: float) -> dict:
    """실시간 변동성 계산"""
    
    # 가격 히스토리 관리
    if "price_history" not in state:
        state["price_history"] = {}
    
    if symbol not in state["price_history"]:
        state["price_history"][symbol] = []
    
    history = state["price_history"][symbol]
    now = time.time()
    
    # 현재 가격 추가
    history.append({"price": current_price, "time": now})
    
    # 오래된 데이터 제거 (60분 윈도우)
    cutoff = now - (VOLATILITY_WINDOW * 60)
    history = [h for h in history if h["time"] > cutoff]
    state["price_history"][symbol] = history
    
    if len(history) < 2:
        return {"max_swing": 0, "current_swing": 0}
    
    prices = [h["price"] for h in history]
    
    # 최근 60분 내 최고/최저
    recent_high = max(prices)
    recent_low = min(prices)
    
    # 최대 변동폭 (고점에서 저점까지)
    max_swing = ((recent_high - recent_low) / recent_low) * 100 if recent_low > 0 else 0
    
    # 현재 위치 (저점 대비)
    current_swing = ((current_price - recent_low) / recent_low) * 100 if recent_low > 0 else 0
    
    return {
        "max_swing": max_swing,
        "current_swing": current_swing,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "samples": len(history)
    }

# ==================== 레벨 판정 ====================
def grade_level(value: float, is_vix: bool = False, is_volatility: bool = False) -> str | None:
    """레벨 판정"""
    a = abs(value)
    
    if is_volatility:
        # 변동성 기반 (일중 스윙)
        if a >= 3.0: return "LV3"
        if a >= 2.0: return "LV2"
        if a >= 1.0: return "LV1"
    else:
        # 일반 지수 (전일 대비)
        if a >= 2.5: return "LV3"
        if a >= 1.5: return "LV2"
        if a >= 0.8: return "LV1"
    return None

def grade_level_vix_relative(change_pct: float) -> str | None:
    """VIX 상대적 변화율 기준 레벨 판정"""
    a = abs(change_pct)
    
    # VIX 변화율 기준 (일반 지수보다 높게 설정)
    if a >= 30.0: return "LV3"  # VIX 30% 이상 변화
    if a >= 20.0: return "LV2"  # VIX 20% 이상 변화  
    if a >= 10.0: return "LV1"  # VIX 10% 이상 변화
    return None

# ==================== 알림 Hint/Action 규칙 ====================
def get_alert_hint_action(session: str, level: str | None) -> tuple[str | None, str | None]:
    """세션과 레벨에 따른 hint/action 생성
    
    Args:
        session: "KR", "KR_FUTURES", "US", "FUTURES"
        level: "LV1", "LV2", "LV3" or None
    
    Returns:
        (hint, action) 튜플
    """
    if not level:
        return (None, None)
    
    # 한국장 (메인 감시 대상)
    if session in ["KR", "KR_FUTURES", "FUTURES"]:
        hints = {
            "LV1": "If 정규장 변동 확대 → 코어 유지, 증거금/델타 체크",
            "LV2": "If 추세 이탈 징후 → 방어 헤지 후보",
            "LV3": "If 급변/리스크 급증 → 우선 생존(증거금/리스크 축소)"
        }
        actions = {
            "LV1": "monitor_delta",
            "LV2": "prep_hedge",
            "LV3": "reduce_risk"
        }
    # 미국장 (부수 감시 - 야간 참고/리스크 시그널)
    else:  # US
        hints = {
            "LV1": "If 하락 지속 + 변동성 확대 → 헤지 준비",
            "LV2": "If 급락 확장/리스크 확대 → 방어적 헤지 고려",
            "LV3": "If 패닉/갭다운 → 리스크 축소 + 헷지 강화"
        }
        actions = {
            "LV1": "prep_hedge",
            "LV2": "activate_hedge",
            "LV3": "reduce_risk"
        }
    
    return (hints.get(level), actions.get(level))


# ==================== 알림 전송 ====================
def post_alert(data: dict, level: str | None, symbol: str, note: str, kind: str = "ALERT"):
    """알림 전송 - 지수 중심 포맷 + hint/action/session/observed_at 확장"""
    is_vix = (symbol == "^VIX")
    is_k200f = (symbol == "K200F")
    
    # K200 선물은 명확한 이름으로
    if is_k200f:
        display_name = "K200 선물"
    else:
        display_name = human_name(symbol)
    
    # 세션 타입 결정 (hint/action 생성용)
    if kind in ["KR", "FUTURES"]:
        session_type = "KR_FUTURES"
    elif kind == "US" or is_vix:
        session_type = "US"
    else:
        session_type = kind
    
    # hint/action 생성
    hint, action = get_alert_hint_action(session_type, level)
    
    # 공통 필드 (optional - 하위호환 유지)
    observed_at_kst = _now_kst_iso()
    
    # VIX는 보조 정보로, 주요 지수 정보를 우선 표시
    if is_vix and "vix_context" in data:
        vix_ctx = data["vix_context"]
        # VIX 알림에서는 S&P500과 NASDAQ 변동을 메인으로 표시
        sp_change = vix_ctx.get("sp500_change", 0)
        nas_change = vix_ctx.get("nasdaq_change", 0)
        
        # 주요 지수명을 메인으로
        primary_index = "S&P 500" if abs(sp_change) > abs(nas_change) else "NASDAQ"
        primary_change = sp_change if abs(sp_change) > abs(nas_change) else nas_change
        
        payload = {
            "index": primary_index,
            "level": level or "INFO",
            "delta_pct": round(primary_change, 2),
            "triggered_at": _now_kst_iso(),
            "note": f"{note} | VIX {vix_ctx['value']:.1f} ({vix_ctx['change_pct']:+.1f}%)",
            "kind": "US",
            "symbol": "^GSPC" if abs(sp_change) > abs(nas_change) else "^IXIC",
            # 확장 필드 (optional)
            "session": "US",
            "observed_at": observed_at_kst,
            "hint": hint,
            "action": action,
            "details": {
                "sp500_change": sp_change,
                "nasdaq_change": nas_change,
                "vix_value": vix_ctx["value"],
                "vix_change": vix_ctx["change_pct"],
                "index_volatility": vix_ctx.get("index_volatility", 0)
            }
        }
    else:
        # 일반 지수 알림 (K200 선물 포함)
        payload = {
            "index": display_name,
            "level": level or "INFO",
            "delta_pct": round(data.get("change_pct", 0), 2),
            "triggered_at": _now_kst_iso(),
            "note": note,
            "kind": kind,
            "symbol": symbol,
            # 확장 필드 (optional)
            "session": session_type,
            "observed_at": observed_at_kst,
            "hint": hint,
            "action": action,
            "details": {
                "current": data.get("current"),
                "high": data.get("high"),
                "low": data.get("low"),
                "volatility": data.get("volatility", {})
            }
        }
    
    headers = {"Content-Type": "application/json"}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY
    
    if not SENTINEL_BASE_URL:
        log.warning("SENTINEL_BASE_URL 미설정 - 알림 스킵")
        return
    
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            log.error("알림 전송 실패 %s %s", r.status_code, r.text)
        else:
            log.info(">>> 알림 전송: [%s] %s %s (%s) hint=%s action=%s", 
                    kind, display_name, level or "INFO", note, hint, action)
    except Exception as e:
        log.error("알림 전송 오류: %s", e)

# ==================== 시장 시간 판정 ====================
# 미국 주요 휴장일 (2025년 기준) - TODO: 매년 업데이트 필요
US_HOLIDAYS_2025 = [
    (1, 1),   # New Year's Day
    (1, 20),  # Martin Luther King Jr. Day (3rd Monday)
    (2, 17),  # Presidents Day (3rd Monday)
    (4, 18),  # Good Friday
    (5, 26),  # Memorial Day (Last Monday)
    (6, 19),  # Juneteenth
    (7, 4),   # Independence Day
    (9, 1),   # Labor Day (1st Monday)
    (11, 27), # Thanksgiving (4th Thursday)
    (12, 25), # Christmas
]

# 한국 공휴일 (2025년 기준) - 실제 휴장일만 포함
KR_HOLIDAYS_2025 = [
    (1, 1),   # 신정
    (1, 28), (1, 29), (1, 30),  # 설연휴
    (3, 1),   # 삼일절
    (5, 5),   # 어린이날 (월요일)
    (5, 6),   # 대체공휴일
    (6, 6),   # 현충일
    (8, 15),  # 광복절
    (10, 3),  # 개천절
    (10, 6), (10, 7), (10, 8),  # 추석연휴
    (12, 25), # 크리스마스
]

def is_us_market_open(now_nyc: datetime = None) -> bool:
    """NYSE 정규장 오픈 여부 판정 (America/New_York 기준)
    
    NYSE 정규장: 월~금 09:30 ~ 16:00 (NY 현지시간)
    주말/공휴일: 휴장
    
    Args:
        now_nyc: NY 현지 시간 (None이면 현재 시간)
    
    Returns:
        bool: 미국장 오픈 여부
    """
    if now_nyc is None:
        now_nyc = _now_nyc()
    
    # NY 기준 주말 체크
    if now_nyc.weekday() >= 5:  # 토요일(5), 일요일(6)
        return False
    
    # NY 기준 공휴일 체크
    if (now_nyc.month, now_nyc.day) in US_HOLIDAYS_2025:
        return False
    
    # NY 정규장: 09:30 ~ 16:00
    ny_hhmm = now_nyc.hour * 100 + now_nyc.minute
    return 930 <= ny_hhmm <= 1600


def current_session() -> str:
    """현재 세션 판정
    
    세션 우선순위:
    1. KR (한국 정규장 09:00~15:30 KST) - 메인 감시 대상
    2. US (미국 정규장 09:30~16:00 NY) - 부수 감시 대상  
    3. FUTURES (한국 야간선물 18:00~05:00 KST + 미국선물)
    4. CLOSED (휴장)
    
    핵심 변경:
    - 미국장 판정은 KST가 아닌 America/New_York 현지시간 기준
    - 토요일 새벽(KST)도 NY 금요일 장중이면 US로 판정
    - 월요일 새벽(KST)은 NY 일요일이므로 US 판정 안함
    """
    now_kst = _now_kst()
    now_nyc = _now_nyc()
    
    kst_hhmm = now_kst.hour * 100 + now_kst.minute
    
    # 1. 한국 정규장 체크 (KST 기준)
    kr_weekday = now_kst.weekday()
    if kr_weekday < 5:  # 평일
        if (now_kst.month, now_kst.day) not in KR_HOLIDAYS_2025:
            if 900 <= kst_hhmm <= 1530:
                return "KR"
    
    # 2. 미국 정규장 체크 (NY 현지시간 기준) - 핵심 수정
    if is_us_market_open(now_nyc):
        return "US"
    
    # 3. 선물 세션 체크 (KST 기준)
    # 한국 야간선물: 18:00 ~ 익일 05:00
    # 조건: KST 평일이고 공휴일이 아닐 때만
    is_kr_futures_time = (kst_hhmm >= 1800) or (kst_hhmm < 500)
    
    if is_kr_futures_time:
        # 야간선물은 전날 정규장 기준 (00:00~05:00은 전날 세션)
        if kst_hhmm < 500:
            ref_day = now_kst.date() - timedelta(days=1)
        else:
            ref_day = now_kst.date()
        
        if ref_day.weekday() < 5 and (ref_day.month, ref_day.day) not in KR_HOLIDAYS_2025:
            return "FUTURES"
    
    # 4. 그 외는 휴장
    return "CLOSED"

# ==================== 메인 감시 로직 ====================
def check_and_alert():
    """메인 감시 로직 - 실시간 변동성 감지"""
    state = _load_state()
    sess = current_session()
    
    # 강제 시장 오픈 환경변수 체크
    FORCE_MARKET_OPEN = os.getenv("FORCE_MARKET_OPEN", "false").lower() in ["true", "1", "yes"]
    
    log.info("="*60)
    log.info("시장 체크 시작 [세션: %s] %s", sess, _now_kst().strftime("%Y-%m-%d %H:%M:%S KST"))
    if FORCE_MARKET_OPEN:
        log.info("🔴 강제 시장 오픈 모드 활성화 - 휴장일에도 감시 계속")
    if K200_FUTURES_ENABLED:
        log.info("📊 K200 선물 감시 활성화 (DB증권 API)")
    log.info("="*60)
    
    state["last_checked_at"] = _now_kst_iso()
    state["last_session"] = sess
    
    # K200 선물 체크 (30분에 한 번)
    last_k200_check = state.get("last_k200_check", 0)
    now_ts = time.time()
    k200_check_needed = (now_ts - last_k200_check) >= (K200_CHECK_INTERVAL * 60)
    
    if K200_FUTURES_ENABLED and k200_check_needed and sess in ["KR", "FUTURES"]:
        log.info("📊 K200 선물 체크 시작...")
        try:
            k200_data = get_k200_futures_data()
            if k200_data:
                current_price = k200_data["current"]
                change_pct = k200_data["change_pct"]
                
                log.info("✓ K200 선물: 현재=%.2f, 변화=%.2f%%", current_price, change_pct)
                
                # 레벨 판정
                abs_change = abs(change_pct)
                level = None
                if abs_change >= 2.5:
                    level = "LV3"
                elif abs_change >= 1.5:
                    level = "LV2"
                elif abs_change >= 0.8:
                    level = "LV1"
                
                # 알림 조건
                state_key = f"K200F_{sess}"
                prev_level = state.get(state_key, {}).get("level")
                
                if level and level != prev_level:
                    direction = "상승" if change_pct > 0 else "하락"
                    note = f"K200 선물 {direction} {abs(change_pct):.2f}% (DB증권 API)"
                    
                    post_alert(k200_data, level, "K200F", note, kind="FUTURES")
                    log.info(">>> K200 선물 알림: [%s] %s", level, note)
                    
                    # 상태 업데이트
                    state[state_key] = {
                        "level": level,
                        "change_pct": change_pct,
                        "updated_at": _now_kst_iso()
                    }
                
                state["last_k200_check"] = now_ts
            else:
                log.warning("⚠ K200 선물 데이터 수집 실패")
        except Exception as e:
            log.error("K200 선물 체크 오류: %s", e)
    
    # 강제 알림 체크
    last_alert_time = state.get("last_alert_time", 0)
    current_time = time.time()
    force_alert = (current_time - last_alert_time) > (FORCE_ALERT_INTERVAL * 3600)
    
    if sess == "CLOSED" and not FORCE_MARKET_OPEN:
        log.info("시장 휴장 중 - 감시 스킵 (강제 모드 비활성화)")
        _save_state(state)
        return
    elif sess == "CLOSED" and FORCE_MARKET_OPEN:
        # 강제 모드에서는 미국 시장 감시
        log.info("🔴 강제 모드: 휴장 중에도 미국 시장 감시")
        sess = "US"
    
    # 세션별 심볼 선택
    if sess == "KR":
        # 한국 시장: KOSPI 메인, ETF는 백업용
        # KOSPI 데이터 먼저 시도, 실패시 ETF 사용
        symbols = [KR_MAIN_INDEX]  # KOSPI만
        session_name = "한국 정규장"
    elif sess == "US":
        symbols = US_SPOT
        session_name = "미국 정규장"
    elif sess == "FUTURES":
        symbols = FUTURES_SYMBOLS  # 미국선물만 (K200선물은 DB증권 API)
        session_name = "선물 시장 (미국)"
    else:
        _save_state(state)
        return
    
    log.info("【%s】 데이터 수집 중...", session_name)
    
    # VIX 필터용 지수 변동 체크 (미국장만)
    max_index_move = 0
    spx_delta = None
    ndx_delta = None
    
    if sess == "US":
        for sym in ["^GSPC", "^IXIC"]:
            data = get_market_data(sym)
            if data and data.get("change_pct") is not None:
                if sym == "^GSPC":
                    spx_delta = data["change_pct"]
                else:
                    ndx_delta = data["change_pct"]
                max_index_move = max(max_index_move, abs(data["change_pct"]))
    
    # 한국 시장 특별 처리: KOSPI 실패시 ETF 백업
    if sess == "KR":
        kospi_data = get_market_data(KR_MAIN_INDEX)
        if not kospi_data or kospi_data.get("current") is None:
            log.warning("⚠ KOSPI 데이터 수집 실패 - ETF 백업 시도")
            # ETF로 백업
            for etf_symbol in KR_ETF_BACKUP:
                etf_data = get_market_data(etf_symbol)
                if etf_data and etf_data.get("current") is not None:
                    log.info("✓ %s 백업 데이터 사용", human_name(etf_symbol))
                    kospi_data = etf_data
                    # KOSPI 이름으로 처리
                    break
        
        # KOSPI 데이터가 있으면 처리
        if kospi_data and kospi_data.get("current") is not None:
            # 한국 지수는 한 번만 처리 (중복 알림 방지)
            symbols = [KR_MAIN_INDEX]
        else:
            log.error("⚠ 한국 시장 데이터 전체 수집 실패")
            _save_state(state)
            return
    
    # 각 심볼 감시
    for symbol in symbols:
        data = get_market_data(symbol)
        if not data or data.get("current") is None:
            log.warning("⚠ %s 데이터 수집 실패", human_name(symbol))
            continue
        
        name = human_name(symbol)
        is_vix = (symbol == "^VIX")
        
        # VIX 특별 처리
        if is_vix:
            vix_value = data["current"]
            vix_change_pct = data.get("change_pct", 0)
            
            # 1. 지수 변동 필터 (0.8% 미만이면 VIX 무시)
            if sess == "US" and max_index_move < VIX_FILTER_THRESHOLD:
                log.info("VIX 필터: 지수 변동 %.2f%% < %.1f%% → VIX %.2f (%.2f%%) 무시", 
                        max_index_move, VIX_FILTER_THRESHOLD, vix_value, vix_change_pct)
                continue
            
            # 2. VIX 변화율 기준 레벨 판정
            current_level = grade_level_vix_relative(vix_change_pct)
            
            # 3. 추가 조건: VIX 절대값이 너무 낮으면(12 미만) 레벨 하향
            if vix_value < 12 and current_level:
                log.info("VIX 절대값 낮음(%.1f) - 레벨 무시", vix_value)
                current_level = None
            
            # 4. 추가 조건: VIX 절대값이 높으면(30 이상) 레벨 상향
            if vix_value >= 30 and not current_level:
                current_level = "LV1"  # 최소 LV1 보장
            
            log.info("✓ VIX: 값=%.2f, 변화율=%.2f%%, 지수변동=%.2f%%, 레벨=%s", 
                    vix_value, vix_change_pct, max_index_move, current_level or "정상")
            
            # 상태 체크
            state_key = f"{sess}_{symbol}"
            prev_state = state.get(state_key, {})
            prev_level = prev_state.get("level")
            
            # 알림 조건
            should_alert = False
            alert_note = ""
            
            # 레벨 변경시 알림
            if current_level != prev_level:
                should_alert = True
                
                if not prev_level and current_level:
                    # 레벨 진입
                    direction = "상승" if vix_change_pct > 0 else "하락"
                    alert_note = f"VIX {current_level} 진입 ({direction} {abs(vix_change_pct):.1f}%)"
                elif prev_level and not current_level:
                    # 레벨 해제
                    alert_note = f"VIX 안정화 (현재 {vix_value:.1f})"
                else:
                    # 레벨 변경
                    alert_note = f"VIX {prev_level} → {current_level} (변화 {vix_change_pct:+.1f}%)"
                
                # 컨텍스트 정보 추가
                if max_index_move >= 0.8 and spx_delta is not None and ndx_delta is not None:
                    sp_direction = "하락" if spx_delta < 0 else "상승"
                    nas_direction = "하락" if ndx_delta < 0 else "상승"
                    alert_note += f" [S&P {sp_direction} {abs(spx_delta):.1f}%, NAS {nas_direction} {abs(ndx_delta):.1f}%]"
            
            # 강제 알림 (4시간마다, 레벨 유지중)
            elif force_alert and current_level:
                should_alert = True
                alert_note = f"VIX {current_level} 유지 중 (현재 {vix_value:.1f}, 변화 {vix_change_pct:+.1f}%)"
            
            if should_alert:
                # VIX 알림에 추가 정보 포함
                data["vix_context"] = {
                    "value": vix_value,
                    "change_pct": vix_change_pct,
                    "index_volatility": max_index_move,
                    "sp500_change": spx_delta,
                    "nasdaq_change": ndx_delta
                }
                post_alert(data, current_level, symbol, alert_note, kind="VIX")
                state["last_alert_time"] = current_time
            
            # 상태 업데이트
            state[state_key] = {
                "level": current_level,
                "vix_value": vix_value,
                "change_pct": vix_change_pct,
                "updated_at": _now_kst_iso()
            }
            
            continue  # VIX 처리 완료, 다음 심볼로
        
        # 일반 지수 처리
        # 변동성 계산
        volatility = calculate_volatility(state, symbol, data["current"])
        data["volatility"] = volatility
        
        # None 값 처리 - 로그 출력 수정
        current_price = data.get("current", 0)
        change_pct = data.get("change_pct", 0) 
        max_swing = volatility.get("max_swing", 0)
        high_price = data.get("high", 0) or 0  # None을 0으로 변환
        low_price = data.get("low", 0) or 0   # None을 0으로 변환
        
        log.info("✓ %s: 현재=%.2f, 전일대비=%.2f%%, 일중변동=%.2f%% (고:%.2f/저:%.2f)", 
                name, 
                current_price,
                change_pct,
                max_swing,
                high_price,
                low_price)
        
        # 상태 키
        state_key = f"{sess}_{symbol}"
        prev_state = state.get(state_key, {})
        
        # 레벨 판정 (다중 기준)
        change_level = grade_level(change_pct, is_vix=False)
        volatility_level = grade_level(max_swing, is_volatility=True)
        
        # 최종 레벨 (더 높은 것 선택)
        levels = [l for l in [change_level, volatility_level] if l]
        if levels:
            level_order = {"LV1": 1, "LV2": 2, "LV3": 3}
            current_level = max(levels, key=lambda x: level_order.get(x, 0))
        else:
            current_level = None
        
        prev_level = prev_state.get("level")
        prev_volatility = prev_state.get("volatility", {}).get("max_swing", 0)
        
        # 알림 조건
        should_alert = False
        alert_note = ""
        
        # 1. 레벨 변경
        if current_level != prev_level:
            should_alert = True
            if not prev_level and current_level:
                alert_note = f"{current_level} 진입"
            elif prev_level and not current_level:
                alert_note = "정상 복귀"
            else:
                alert_note = f"{prev_level} → {current_level}"
            
            # 변동성 정보 추가
            if max_swing >= 1.0:
                alert_note += f" (일중 {max_swing:.1f}% 변동)"
        
        # 2. 급격한 변동성 증가
        elif max_swing - prev_volatility >= 1.0:
            should_alert = True
            alert_note = f"변동성 급증: {prev_volatility:.1f}% → {max_swing:.1f}%"
        
        # 3. 강제 알림
        elif force_alert and current_level:
            should_alert = True
            alert_note = f"정기: {current_level} 유지 중"
        
        # 4. 선물 특별 처리
        elif sess == "FUTURES" and abs(change_pct) >= 0.8:
            # 선물 알림 중복 방지 개선
            futures_key = f"futures_{symbol}_{_now_kst().strftime('%Y%m%d')}"
            if futures_key not in state.get("futures_alerted", {}):
                should_alert = True
                alert_note = f"선물 {'상승' if change_pct > 0 else '하락'} {abs(change_pct):.2f}%"
                if "futures_alerted" not in state:
                    state["futures_alerted"] = {}
                state["futures_alerted"][futures_key] = current_time
        
        if should_alert:
            post_alert(data, current_level, symbol, alert_note, kind=sess)
            state["last_alert_time"] = current_time
        
        # 상태 업데이트
        state[state_key] = {
            "level": current_level,
            "change_pct": change_pct,
            "volatility": volatility,
            "updated_at": _now_kst_iso()
        }
    
    _save_state(state)
    log.info("체크 완료")
    log.info("-"*60)

# ==================== Cron Job용 단일 실행 ====================
async def check_and_alert_once():
    """
    시장 감시를 한 번만 실행하고 종료하는 함수 (Cron Job용)
    Railway Cron Job에서 30분마다 이 스크립트를 실행
    """
    log.info("="*60)
    log.info("Sentinel 시장감시 시작 (Cron Job 단일 실행)")
    log.info("="*60)
    log.info("설정:")
    log.info("  - 전일대비: 0.8% / 1.5% / 2.5%")
    log.info("  - 일중변동: 1.0% / 2.0% / 3.0%")
    log.info("  - VIX 변화율: 10% / 20% / 30%")
    log.info("  - VIX 필터: 지수 %.1f%% 미만 변동시 무시", VIX_FILTER_THRESHOLD)
    log.info("  - 변동성 윈도우: %d분", VOLATILITY_WINDOW)
    log.info("-"*60)
    
    try:
        # 한 번만 체크하고 종료
        check_and_alert()
        log.info("✅ 시장 감시 완료 - 프로세스 종료")
    except Exception as e:
        log.error("❌ 시장 감시 오류: %s", e, exc_info=True)
        raise  # 에러를 Cron Job 시스템에 전달

if __name__ == "__main__":
    # asyncio.run()을 사용하여 한 번만 실행
    asyncio.run(check_and_alert_once())
