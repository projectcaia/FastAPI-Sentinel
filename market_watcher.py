# market_watcher.py — FGPT Sentinel 시장감시 워커 (v2: 30분 주기 + 레벨변경시만 알림 + KR/US 세션 구분)
# - 주기: 기본 30분 (WATCH_INTERVAL_SEC=1800)
# - 알림 정책: 레벨이 "충족/강화/약화/해제" 될 때만 전송 (동일 레벨 유지 시 미전송)
# - 세션: KST 기준 주간(KR: ΔK200) / 야간(US: S&P500, NASDAQ, VIX) 자동 전환
# - Fallback: ΔK200은 ^KS200 → (069500.KS, 102110.KS) 평균 → ^KS11
#
# 필요 환경변수(.env):
#   SENTINEL_BASE_URL=https://fastapi-sentinel-production.up.railway.app
#   SENTINEL_KEY=change_this_to_a_long_random_string   # (선택)
#   WATCH_INTERVAL_SEC=1800
#   LOG_LEVEL=INFO
#
# 상태 저장:
#   /mnt/data/market_state.json 에 마지막 레벨 상태를 저장하여 레벨변경 감지

import os, time, json, logging, requests
from datetime import datetime, timezone, timedelta

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("market-watcher")

SENTINEL_BASE_URL = os.getenv("SENTINEL_BASE_URL", "https://fastapi-sentinel-production.up.railway.app").rstrip("/")
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "").strip()
# 환경변수에서 숫자만 추출 (설명 텍스트 제거)
def parse_int_env(key: str, default: int) -> int:
    """환경변수를 정수로 파싱 (설명 텍스트 자동 제거)"""
    value = os.getenv(key, str(default))
    import re
    match = re.search(r'\d+', value)
    if match:
        return int(match.group())
    return default

WATCH_INTERVAL    = parse_int_env("WATCH_INTERVAL_SEC", 1800)  # 30분 기본 (충분한 간격)
STATE_PATH        = os.getenv("WATCHER_STATE_PATH", "./market_state.json")  # 로컬 디렉토리로 변경

UA = {"User-Agent": "Mozilla/5.0 (FGPT-Caia MarketWatcher)"}

# ΔK200용 심볼 우선순위
SYMBOL_PRIMARY    = "^KS200"
SYMBOL_FALLBACKS  = ["069500.KS", "102110.KS", "^KS11"]  # KODEX200, TIGER200, KOSPI 본지수

# US 세션 심볼
SYMBOL_SPX   = "^GSPC"
SYMBOL_NDX   = "^IXIC"
SYMBOL_VIX   = "^VIX"

def _now_kst():
    return datetime.now(timezone(timedelta(hours=9)))

def _now_kst_iso():
    return _now_kst().isoformat(timespec="seconds")

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

def _yahoo_quote(symbols):
    if isinstance(symbols, (list, tuple)):
        symbols_param = ",".join(symbols)
    else:
        symbols_param = symbols
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    r = requests.get(url, params={"symbols": symbols_param}, headers=UA, timeout=10)
    r.raise_for_status()
    return r.json()

def _extract_change_percent(q: dict):
    cp = q.get("regularMarketChangePercent")
    if cp is not None:
        return float(cp)
    price = q.get("regularMarketPrice")
    prev  = q.get("regularMarketPreviousClose")
    if price is not None and prev not in (None, 0):
        return (float(price) - float(prev)) / float(prev) * 100.0
    return None

def get_delta_k200() -> tuple[float, str]:
    # 1) ^KS200
    try:
        data = _yahoo_quote([SYMBOL_PRIMARY])
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            cp = _extract_change_percent(items[0])
            if cp is not None:
                return float(cp), SYMBOL_PRIMARY
    except Exception as e:
        log.warning("primary ^KS200 실패: %s", e)

    # 2) ETF 평균
    try:
        data = _yahoo_quote(SYMBOL_FALLBACKS[:2])
        items = data.get("quoteResponse", {}).get("result", [])
        vals = []
        for it in items:
            cp = _extract_change_percent(it)
            if cp is not None:
                vals.append(float(cp))
        if vals:
            avg = sum(vals) / len(vals)
            return avg, "ETF_AVG(069500.KS,102110.KS)"
    except Exception as e:
        log.warning("ETF 평균 실패: %s", e)

    # 3) KOSPI 본지수
    try:
        data = _yahoo_quote([SYMBOL_FALLBACKS[-1]])
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            cp = _extract_change_percent(items[0])
            if cp is not None:
                return float(cp), SYMBOL_FALLBACKS[-1]
    except Exception as e:
        log.warning("KOSPI 본지수 ^KS11 실패: %s", e)

    raise RuntimeError("ΔK200 추정 실패: 모든 소스에서 데이터 없음")

def get_delta(symbol) -> float:
    data = _yahoo_quote([symbol])
    items = data.get("quoteResponse", {}).get("result", [])
    if not items:
        raise RuntimeError(f"{symbol} 데이터 없음")
    cp = _extract_change_percent(items[0])
    if cp is None:
        raise RuntimeError(f"{symbol} 변동률 계산 불가")
    return float(cp)

def grade_level(delta_pct: float) -> str | None:
    a = abs(delta_pct)
    if a >= 1.5:
        return "LV3"
    if a >= 1.0:
        return "LV2"
    if a >= 0.4:
        return "LV1"
    return None

def post_alert(index_name: str, delta_pct: float, level: str | None, source_tag: str, note: str):
    # level이 None이면 '해제' 알림으로 처리(레벨 해제)
    # 볼린저 밴드 이벤트는 별도 처리
    if level in ["BREACH", "RECOVER", "CLEARED"]:
        # 볼린저 밴드 이벤트는 LV2로 매핑
        display_level = "LV2" if level != "CLEARED" else level
        note = f"[BB {level}] " + note
    else:
        display_level = level if level else "CLEARED"
    
    payload = {
        "index": index_name,
        "level": display_level,
        "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "triggered_at": _now_kst_iso(),
        "note": note + f" [{source_tag}]"
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

def current_session() -> str:
    """KST 기준 세션 구분: KR(주간 08:30~16:00), US(야간 그 외 전 시간)."""
    now = _now_kst()
    hhmm = now.hour * 100 + now.minute
    weekday = now.weekday()  # 0=월, 6=일
    
    # 주말은 항상 US 세션으로 처리
    if weekday >= 5:  # 토요일(5), 일요일(6)
        return "US"
    
    if 830 <= hhmm <= 1600:
        return "KR"
    return "US"


# === Bollinger(±2σ) 급등·급락 감지 추가 ==========================================
# - Yahoo chart API로 최근 시세를 받아 20기간 평균/표준편차로 z-score 계산
# - |z| >= K_SIGMA(기본 1.0)이면 'BREACH' (밴드 이탈), 이전이 이탈이었는데 |z|<K면 'RECOVER'
# - KR 세션: ^KS200→069500.KS→102110.KS→^KS11 순으로 시도
# - US 세션: ^GSPC, ^IXIC, ^VIX 각각 적용
# - 상태 키: "BB_<INDEX>" (예: BB_ΔK200, BB_ΔSPX, BB_ΔNASDAQ, BB_ΔVIX)

import math

# float 환경변수도 안전하게 파싱
def parse_float_env(key: str, default: float) -> float:
    """환경변수를 실수로 파싱 (설명 텍스트 자동 제거)"""
    value = os.getenv(key, str(default))
    import re
    match = re.search(r'[\d.]+', value)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return default
    return default

K_SIGMA = parse_float_env("BOLL_K_SIGMA", 2.0)  # 2.0 시그마 기본값 (안정적)
BB_WINDOW = parse_int_env("BOLL_WINDOW", 20)

def _yahoo_chart(symbol: str, rng: str, interval: str):
    """Yahoo chart API 호출, (timestamps, closes) 반환"""
    import requests
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": rng, "interval": interval}
    r = requests.get(url, params=params, headers=UA, timeout=12)
    r.raise_for_status()
    data = r.json()
    res = data.get("chart", {}).get("result", [])
    if not res:
        raise RuntimeError(f"chart API no result for {symbol}")
    res0 = res[0]
    closes = res0.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    ts = res0.get("timestamp", [])
    if not closes or not ts or len(closes) != len(ts):
        raise RuntimeError(f"chart incomplete for {symbol}")
    # 필터: None 제거
    series = [(t, c) for t, c in zip(ts, closes) if c is not None]
    if len(series) < BB_WINDOW + 2:
        raise RuntimeError(f"not enough points for {symbol} ({len(series)})")
    return [t for t, _ in series], [c for _, c in series]

def _zscore_latest(closes: list[float], window: int) -> tuple[float, float]:
    """마지막값 z, 직전값 z_prev 반환"""
    if len(closes) < window + 1:
        raise RuntimeError("insufficient closes for zscore")
    import statistics as stats
    last = closes[-1]
    prev = closes[-2]
    base = closes[-(window+1):-1]  # 직전 window
    mu = sum(base) / len(base)
    # 표준편차: population std vs sample std → 표본표준편차 사용
    variance = sum((x - mu) ** 2 for x in base) / (len(base) - 1)
    sd = math.sqrt(variance) if variance > 0 else 0.0
    if sd == 0:
        return 0.0, 0.0
    z_last = (last - mu) / sd
    z_prev = (prev - mu) / sd
    return z_last, z_prev

def bb_event_for_symbol(symbol: str, rngs=("5d","1mo"), intervals=("30m","60m","1d")):
    """여러 설정을 시도하여 z-score 산출. 반환: (event, z, used_cfg)
       event ∈ {"BREACH","RECOVER",None}
    """
    last_err = None
    for rng in rngs:
        for iv in intervals:
            try:
                _, closes = _yahoo_chart(symbol, rng, iv)
                z, z_prev = _zscore_latest(closes, BB_WINDOW)
                # 이벤트 판정
                breach_now = abs(z) >= K_SIGMA
                breach_prev = abs(z_prev) >= K_SIGMA
                if breach_now and not breach_prev:
                    return "BREACH", z, f"{symbol}:{rng}/{iv}"
                if (not breach_now) and breach_prev:
                    return "RECOVER", z, f"{symbol}:{rng}/{iv}"
                return None, z, f"{symbol}:{rng}/{iv}"
            except Exception as e:
                last_err = e
                continue
    if last_err:
        raise last_err
    return None, 0.0, f"{symbol}:n/a"

def check_and_alert_bb(state: dict, sess: str):
    """세션별 볼린저 급등·급락 이벤트 감지 및 상태 전이 시 알림"""
    if sess == "KR":
        bb_candidates = [SYMBOL_PRIMARY, "069500.KS", "102110.KS", "^KS11"]
        idx_name = "BB_ΔK200"
        for sym in bb_candidates:
            try:
                ev, z, used = bb_event_for_symbol(sym)
                prev = state.get(idx_name)  # "BREACH" / "RECOVER" / None
                if ev and ev != prev:
                    # 레벨 대신 상태 문자열 사용, index명은 별도로 구분
                    # 볼린저 밴드 이벤트 알림
                    bb_note = f"KR 세션: Bollinger ±{K_SIGMA}σ {'돌파' if ev == 'BREACH' else '회복'} (z={z:.2f})"
                    post_alert(index_name="ΔK200_VOL",
                               delta_pct=round(z, 2),
                               level=ev,  # "BREACH" or "RECOVER"
                               source_tag=used,
                               note=bb_note)
                    state[idx_name] = ev
                elif ev is None and prev:  # 상태 유지 → 알림 없음
                    pass
                break  # 어떤 심볼이든 성공하면 종료
            except Exception as e:
                log.debug("KR BB 후보 실패 %s: %s", sym, e)
                continue
    else:
        us_list = [("BB_ΔSPX","ΔSPX_VOL", SYMBOL_SPX, "US S&P500"),
                   ("BB_ΔNASDAQ","ΔNASDAQ_VOL", SYMBOL_NDX, "US NASDAQ"),
                   ("BB_ΔVIX","ΔVIX_VOL", SYMBOL_VIX, "US VIX")]
        for state_key, idx_name, sym, label in us_list:
            try:
                ev, z, used = bb_event_for_symbol(sym)
                prev = state.get(state_key)
                if ev and ev != prev:
                    # US 볼린저 밴드 이벤트 알림
                    bb_note = f"{label}: Bollinger ±{K_SIGMA}σ {'돌파' if ev == 'BREACH' else '회복'} (z={z:.2f})"
                    post_alert(index_name=idx_name,
                               delta_pct=round(z, 2),
                               level=ev,
                               source_tag=used,
                               note=bb_note)
                    state[state_key] = ev
            except Exception as e:
                log.debug("US BB 실패 %s: %s", sym, e)
                continue
    return state
# === Bollinger spike detection end ===========================================
def check_and_alert():
    state = _load_state()  # { "ΔK200": "LV1", "ΔSPX": "LV2", ... }

    sess = current_session()
    if sess == "KR":
        # ΔK200
        try:
            delta, tag = get_delta_k200()
            lvl = grade_level(delta)
            prev = state.get("ΔK200")
            if lvl != prev:
                note = "KR 세션: 레벨 변경" if prev else "KR 세션: 레벨 진입"
                if prev and not lvl:
                    note = "KR 세션: 레벨 해제"
                elif prev and lvl:
                    # 강화/약화 표시
                    note = f"KR 세션: 레벨 변화 {prev}→{lvl}"
                post_alert("ΔK200", delta, lvl, tag, note)
                state["ΔK200"] = lvl
        except Exception as e:
            log.warning("KR ΔK200 수집/판정 실패: %s", e)
    else:
        # US: S&P500, NASDAQ, VIX
        targets = [
            ("ΔSPX", SYMBOL_SPX,  "US 세션: S&P500"),
            ("ΔNASDAQ", SYMBOL_NDX, "US 세션: NASDAQ"),
            ("ΔVIX", SYMBOL_VIX,  "US 세션: VIX")
        ]
        for idx_name, sym, label in targets:
            try:
                delta = get_delta(sym)
                lvl = grade_level(delta)
                prev = state.get(idx_name)
                if lvl != prev:
                    note = f"{label} 레벨 변경" if prev else f"{label} 레벨 진입"
                    if prev and not lvl:
                        note = f"{label} 레벨 해제"
                    elif prev and lvl:
                        note = f"{label} 레벨 변화 {prev}→{lvl}"
                    post_alert(idx_name, delta, lvl, sym, note)
                    state[idx_name] = lvl
            except Exception as e:
                log.warning("%s 수집/판정 실패: %s", label, e)


    # --- Bollinger(±2σ) 급등·급락 이벤트 병렬 감지 ---
    try:
        state = check_and_alert_bb(state, sess)
    except Exception as _bb_e:
        log.debug("BB 이벤트 처리 실패: %s", _bb_e)

    _save_state(state)

def run_loop():
    log.info("시장감시 워커 시작: interval=%ss, base=%s", WATCH_INTERVAL, SENTINEL_BASE_URL)
    log.info("정책: %d초 주기, 레벨 변경시에만 알림, KR/US 자동 전환", WATCH_INTERVAL)
    log.info("볼린저 밴드: ±%.1fσ 기준, %d기간 이동평균", K_SIGMA, BB_WINDOW)
    
    # 초기 실행으로 즉시 체크
    log.info("초기 시장 체크 실행...")
    try:
        check_and_alert()
        log.info("초기 시장 체크 완료")
    except Exception as e:
        log.error("초기 체크 실패: %s", e)
    while True:
        try:
            check_and_alert()
        except Exception as e:
            log.error("주기 실행 오류: %s", e)
        time.sleep(WATCH_INTERVAL)

if __name__ == "__main__":
    run_loop()
