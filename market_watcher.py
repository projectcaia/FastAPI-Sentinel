#!/usr/bin/env python3
"""
Market Watcher — Restore Pack (2025-08-25)
- Default: send to SENTINEL_URL (/sentinel/alert), NO HMAC (legacy compatible)
- Optional: set BRIDGE_MODE=hub to send to HUB_URL (/bridge/ingest) with HMAC
- 30-min slot aligned (00, 30), send-on-change (level changed) -> 1 message per slot
"""
import os, time, json, logging, requests, hmac, hashlib, random, pathlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

# ==== ENV ====
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO").upper()
WATCH_SECS  = int(os.getenv("WATCH_INTERVAL_SEC", "1800"))
ALIGN_SLOTS = os.getenv("ALIGN_SLOTS","true").lower() in ("1","true","yes")
SEND_MODE   = os.getenv("SEND_MODE","on_change").lower()  # on_change | every_slot
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS","yfinance,yahoo").split(",") if s.strip()]
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY","").strip()
YF_ENABLED  = os.getenv("YF_ENABLED","true").lower() in ("1","true","yes")
USE_PROXY_TICKERS = os.getenv("USE_PROXY_TICKERS","true").lower() in ("1","true","yes")

# Legacy sentinel mode (default)
BRIDGE_MODE = os.getenv("BRIDGE_MODE","sentinel").lower()   # sentinel|hub
SENTINEL_URL = os.getenv("SENTINEL_URL","").strip() or "https://fastapi-sentinel-production.up.railway.app/sentinel/alert"

# Hub mode (optional)
HUB_URL = os.getenv("HUB_URL","").strip()
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET","").strip()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(levelname)s:market-watcher:%(message)s")
log = logging.getLogger("market-watcher")

# ==== time/slot (KST) ====
KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(KST)

def current_slot(dt: Optional[datetime]=None) -> Tuple[str,int]:
    dt = dt or now_kst()
    if not ALIGN_SLOTS:
        base = int(dt.timestamp() // WATCH_SECS)
        slot_start_ts = base * WATCH_SECS
        slot_dt = datetime.fromtimestamp(slot_start_ts, KST)
        return slot_dt.strftime("%Y%m%d%H%M"), WATCH_SECS
    m = 0 if dt.minute < 30 else 30
    slot_dt = dt.replace(minute=m, second=0, microsecond=0)
    return slot_dt.strftime("%Y%m%d%H%M"), WATCH_SECS

def sleep_until_next_slot():
    dt = now_kst()
    _, interval = current_slot(dt)
    if not ALIGN_SLOTS:
        time.sleep(interval); return
    if dt.minute < 30:
        next_dt = dt.replace(minute=30, second=0, microsecond=0)
    else:
        next_dt = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    delta = (next_dt - dt).total_seconds()
    time.sleep(max(1.0, delta))

# ==== send util ====
def _hmac_sig(secret: str, body: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()

def _push(body: dict) -> None:
    raw = json.dumps(body, ensure_ascii=False)
    headers = {"Content-Type":"application/json"}
    url = SENTINEL_URL
    if BRIDGE_MODE == "hub":
        if not HUB_URL or not CONNECTOR_SECRET:
            raise SystemExit("HUB mode selected but HUB_URL/CONNECTOR_SECRET missing")
        url = HUB_URL
        headers["X-Signature"] = _hmac_sig(CONNECTOR_SECRET, raw)
        headers["Idempotency-Key"] = body.get("idempotency_key","")
    backoff = 0.5
    for attempt in range(1,4):
        try:
            r = requests.post(url, data=raw, headers=headers, timeout=15)
            if r.status_code == 200:
                log.info("전송 완료(%s): %s", BRIDGE_MODE, body.get("idempotency_key"))
                return
            else:
                raise RuntimeError(f"{url} -> {r.status_code}: {r.text[:200]}")
        except Exception as e:
            if attempt >= 3:
                raise
            time.sleep(backoff); backoff *= 2

# ==== providers ====
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf  # type: ignore
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance import failed (disabled): %s", e)

_UA_LIST = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
]
_YHDR = {"User-Agent": _UA_LIST[0], "Accept":"application/json, text/plain, */*", "Referer":"https://finance.yahoo.com/"}

def _av_change_percent(symbol: str) -> float:
    if not ALPHAVANTAGE_API_KEY:
        raise RuntimeError("ALPHAVANTAGE_API_KEY not set")
    proxies = {"^GSPC":"SPY","^IXIC":"QQQ","^VIX":"VIXY","ES=F":"SPY","NQ=F":"QQQ","SPY":"SPY","QQQ":"QQQ","^KS200":"069500.KS"}
    sym = proxies.get(symbol, symbol)
    url = "https://www.alphavantage.co/query"
    params = {"function":"GLOBAL_QUOTE","symbol":sym,"apikey":ALPHAVANTAGE_API_KEY}
    r = requests.get(url, params=params, headers={"User-Agent": _YHDR["User-Agent"]}, timeout=12)
    r.raise_for_status()
    data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    q = data.get("Global Quote") or {}
    cp = q.get("10. change percent")
    if cp: return float(str(cp).strip().rstrip("%"))
    price = q.get("05. price"); prev = q.get("08. previous close")
    if price and prev: return (float(price)-float(prev))/float(prev)*100.0
    raise RuntimeError("alphavantage invalid quote")

def _yf_change_percent(symbol: str) -> float:
    if not _YF_READY: raise RuntimeError("yfinance not ready")
    import yfinance as yf  # type: ignore
    t = yf.Ticker(symbol)
    try:
        fi = getattr(t, "fast_info", None)
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        if last and prev: return (float(last)-float(prev))/float(prev)*100.0
    except Exception: pass
    hist = t.history(period="2d", interval="1d")
    if len(hist) >= 2:
        prev = float(hist["Close"].iloc[-2]); last = float(hist["Close"].iloc[-1])
        return (last - prev) / prev * 100.0
    raise RuntimeError("yfinance insufficient data")

def _yahoo_change_percent(symbol: str) -> float:
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    r = requests.get(url, params={"symbols":symbol}, headers=_YHDR, timeout=12)
    r.raise_for_status()
    j = r.json(); items = j.get("quoteResponse",{}).get("result",[])
    if not items: raise RuntimeError("yahoo empty")
    it = items[0]; cp = it.get("regularMarketChangePercent")
    if cp is not None: return float(cp)
    price = it.get("regularMarketPrice"); prev = it.get("regularMarketPreviousClose")
    if price and prev: return (float(price)-float(prev))/float(prev)*100.0
    raise RuntimeError("yahoo cp none")

def _chain_change_percent(symbol: str) -> float:
    last_err = None
    for p in DATA_PROVIDERS:
        try:
            if p == "alphavantage": return _av_change_percent(symbol)
            if p == "yfinance":     return _yf_change_percent(symbol)
            if p == "yahoo":        return _yahoo_change_percent(symbol)
            raise RuntimeError(f"unknown provider {p}")
        except Exception as e:
            last_err = e
            log.debug("provider %s failed for %s: %s", p, symbol, e)
            continue
    if last_err: raise last_err
    raise RuntimeError("no provider worked")

def _grade(delta: float, is_vix=False) -> Optional[str]:
    a = abs(delta)
    if is_vix:
        if a >= 10: return "LV3"
        if a >= 7:  return "LV2"
        if a >= 5:  return "LV1"
    else:
        if a >= 2.5: return "LV3"
        if a >= 1.5: return "LV2"
        if a >= 0.8: return "LV1"
    return None

def _is_us_market_open_kst() -> bool:
    dt = now_kst()
    m = dt.month; h, mi = dt.hour, dt.minute
    is_dst = 3 <= m <= 11
    return (h > (22 if is_dst else 23) or (h == (22 if is_dst else 23) and mi >= 30)) or (h < (5 if is_dst else 6))

# ==== state ====
STATE_PATH = os.getenv("STATE_PATH","/tmp/market_watcher_state.json")
def load_state() -> dict:
    try:
        if pathlib.Path(STATE_PATH).exists():
            return json.loads(pathlib.Path(STATE_PATH).read_text())
    except Exception: pass
    return {"prev_level": None, "last_sent_slot": None}
def save_state(state: dict) -> None:
    try: pathlib.Path(STATE_PATH).write_text(json.dumps(state))
    except Exception: pass

# ==== build & run ====
def build_body(slot: str, index: str, rule: str, level: str, d1: float, d2: float) -> dict:
    ts = now_kst().isoformat(timespec="seconds")
    idem = f"MW-{slot}-{index}-{level}"
    return {
        "idempotency_key": idem,
        "source": "sentinel",
        "type": "alert.market",
        "priority": "high" if level in ("LV2","LV3") else "normal",
        "timestamp": ts,
        "payload": {
            "rule": rule,
            "index": index,
            "level": level,
            "metrics": {"dK200": round(d1,2), "dVIX": round(d2,2)}
        }
    }

def run_once():
    slot, _ = current_slot()
    dt = now_kst()
    kr_open = (dt.weekday() < 5) and (830 <= dt.hour*100 + dt.minute <= 1600)
    us_open = _is_us_market_open_kst()

    symbols: List[str] = []
    if kr_open:
        symbols = ["^KS200"]
    else:
        if us_open: symbols = ["^GSPC","^IXIC","^VIX"]
        else: symbols = ["SPY","QQQ"] if USE_PROXY_TICKERS else ["ES=F","NQ=F"]

    deltas: Dict[str, float] = {}
    for sym in symbols:
        try:
            deltas[sym] = _chain_change_percent(sym)
        except Exception as e:
            logging.getLogger("market-watcher").warning("수집 실패 %s: %s", sym, e)

    k200_proxy = deltas.get("^GSPC") or deltas.get("SPY") or 0.0
    vix_delta  = deltas.get("^VIX") if "^VIX" in symbols else 0.0
    level = _grade(k200_proxy, is_vix=False) or "LV0"

    state = load_state()
    should_send = True
    if SEND_MODE == "on_change":
        should_send = (state.get("prev_level") != level) and (level != "LV0")
    if state.get("last_sent_slot") == slot:
        should_send = False

    if should_send:
        body = build_body(slot, index="KOSPI200", rule="iv_spike", level=level, d1=k200_proxy, d2=vix_delta)
        _push(body)
        state["last_sent_slot"] = slot
        state["prev_level"] = level
        save_state(state)
        log.info("전송됨: slot=%s level=%s ΔK200=%.2f ΔVIX=%.2f", slot, level, k200_proxy, vix_delta)
    else:
        state["prev_level"] = level
        save_state(state)
        log.info("전송 안 함: slot=%s mode=%s level=%s", slot, SEND_MODE, level)

def main():
    log.info("시장감시 시작: interval=%ss align=%s mode=%s providers=%s mode=%s URL=%s",
             WATCH_SECS, ALIGN_SLOTS, SEND_MODE, ",".join(DATA_PROVIDERS),
             BRIDGE_MODE, (HUB_URL if BRIDGE_MODE=='hub' else SENTINEL_URL))
    while True:
        try:
            run_once()
        except Exception as e:
            log.warning("주기 오류: %s", e)
        sleep_until_next_slot()

if __name__ == "__main__":
    main()
