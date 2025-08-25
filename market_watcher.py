#!/usr/bin/env python3
import os, time, json, logging, requests, hmac, hashlib, random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

# ====== ENV ======
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO").upper()
WATCH_SECS  = int(os.getenv("WATCH_INTERVAL_SEC", "1800"))
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS","yfinance,yahoo").split(",") if s.strip()]
YF_ENABLED  = os.getenv("YF_ENABLED","true").lower() in ("1","true","yes")
USE_PROXY_TICKERS = os.getenv("USE_PROXY_TICKERS","true").lower() in ("1","true","yes")

SENTINEL_BASE_URL = os.getenv("SENTINEL_BASE_URL","" ).strip().rstrip("/")
if SENTINEL_BASE_URL and not SENTINEL_BASE_URL.startswith(("http://","https://")):
    SENTINEL_BASE_URL = "https://" + SENTINEL_BASE_URL

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("market-watcher")

# ====== Yahoo 세션 부트스트랩 ======
_UA_LIST = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
]

COMMON_HEADERS = {
    "User-Agent": random.choice(_UA_LIST),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Connection": "close",
}

def yahoo_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(COMMON_HEADERS)
    try:
        s.get("https://finance.yahoo.com/", timeout=10)
    except Exception:
        pass
    return s

_YH_SES: Optional[requests.Session] = None

def _yh() -> requests.Session:
    global _YH_SES
    if _YH_SES is None:
        _YH_SES = yahoo_session()
    return _YH_SES

def _http_get(url, params=None, timeout=12, max_retry=3):
    last = None
    for i in range(max_retry):
        try:
            r = _yh().get(url, params=params, timeout=timeout)
            if r.status_code == 401:
                log.debug("Yahoo 401 → 세션 재부팅")
                time.sleep(0.5)
                globals()["_YH_SES"] = yahoo_session()
                raise requests.HTTPError(response=r)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            sc = getattr(e.response, "status_code", None)
            if sc in (401,429) or (sc and sc >= 500):
                time.sleep(2 ** i)
                last = e
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(1 + i)
            continue
    if last: raise last
    raise RuntimeError("http get failed")

# ====== Providers ======
_YF_READY = False
if YF_ENABLED:
    try:
        import yfinance as yf  # type: ignore
        _YF_READY = True
    except Exception as e:
        log.warning("yfinance import failed (disabled): %s", e)

def _yf_change_percent(symbol: str) -> float:
    if not _YF_READY:
        raise RuntimeError("yfinance not ready")
    import yfinance as yf  # type: ignore
    t = yf.Ticker(symbol)
    try:
        fi = getattr(t, "fast_info", None)
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        if last is not None and prev not in (None, 0):
            return (float(last) - float(prev)) / float(prev) * 100.0
    except Exception:
        pass
    try:
        hist = t.history(period="2d", interval="1d")
        if hist is not None and len(hist) >= 2:
            prev = float(hist["Close"].iloc[-2])
            last = float(hist["Close"].iloc[-1])
            if prev != 0:
                return (last - prev) / prev * 100.0
    except Exception:
        pass
    raise RuntimeError("yfinance insufficient data")

def _yahoo_change_percent(symbol: str) -> float:
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    j = _http_get(url, params={"symbols":symbol}).json()
    items = j.get("quoteResponse",{}).get("result",[])
    if not items:
        raise RuntimeError("yahoo empty")
    it = items[0]
    cp = it.get("regularMarketChangePercent")
    if cp is not None:
        return float(cp)
    price = it.get("regularMarketPrice"); prev = it.get("regularMarketPreviousClose")
    if price is not None and prev not in (None,0):
        return (float(price) - float(prev)) / float(prev) * 100.0
    raise RuntimeError("yahoo cp none")

def _chain_change_percent(symbol: str) -> float:
    last_err = None
    for p in DATA_PROVIDERS:
        try:
            if p == "yfinance": return _yf_change_percent(symbol)
            if p == "yahoo":    return _yahoo_change_percent(symbol)
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
    now = datetime.now(timezone(timedelta(hours=9)))
    m = now.month
    is_dst = 3 <= m <= 11
    h, mi = now.hour, now.minute
    if is_dst:
        return (h > 22 or (h == 22 and mi >= 30)) or (h < 5)
    else:
        return (h > 23 or (h == 23 and mi >= 30)) or (0 <= h < 6)

def run_once():
    now = datetime.now(timezone(timedelta(hours=9)))
    kr_open = (now.weekday() < 5) and (830 <= now.hour*100 + now.minute <= 1600)
    us_open = _is_us_market_open_kst()

    symbols: List[str] = []
    if kr_open:
        symbols = ["^KS200"]
    else:
        if us_open:
            symbols = ["^GSPC","^IXIC","^VIX"]
        else:
            symbols = ["SPY","QQQ"] if USE_PROXY_TICKERS else ["ES=F","NQ=F"]

    deltas: Dict[str, float] = {}
    for sym in symbols:
        try:
            deltas[sym] = _chain_change_percent(sym)
        except Exception as e:
            log.warning("수집 실패 %s: %s", sym, e)

    k200_proxy = deltas.get("^GSPC") or deltas.get("SPY") or 0.0
    vix_delta  = deltas.get("^VIX") if "^VIX" in symbols else 0.0
    level = _grade(k200_proxy, is_vix=False) or "LV0"

    body = {
        "idempotency_key": f"MW-{int(time.time())}-K200",
        "source": "sentinel",
        "type": "alert.market",
        "priority": "high" if level in ("LV2","LV3") else "normal",
        "timestamp": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
        "payload": {
            "rule": "iv_spike",
            "index": "KOSPI200",
            "level": level,
            "metrics": {"dK200": round(k200_proxy,2), "dVIX": round(vix_delta,2)}
        }
    }
    log.info("워크 전송 바디: %s", body)

def main():
    log.info("시장감시 시작: 주기=%ss providers=%s base=%s", WATCH_SECS, ",".join(DATA_PROVIDERS), SENTINEL_BASE_URL or "(unset)")
    backoff = 5
    while True:
        try:
            run_once()
            time.sleep(WATCH_SECS)
            backoff = 5
        except Exception as e:
            log.warning("주기 오류: %s", e)
            time.sleep(backoff)
            backoff = min(backoff*2, 600)

if __name__ == "__main__":
    main()
