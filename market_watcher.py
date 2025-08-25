#!/usr/bin/env python3
import os, time, json, logging, requests, math, hmac, hashlib
from datetime import datetime, timezone, timedelta

# -------- Config (ENV) --------
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO").upper()
WATCH_SECS  = int(os.getenv("WATCH_INTERVAL_SEC", "1800"))
DATA_PROVIDERS = [s.strip().lower() for s in os.getenv("DATA_PROVIDERS","alphavantage,yfinance,yahoo").split(",") if s.strip()]
YF_ENABLED  = os.getenv("YF_ENABLED","true").lower() in ("1","true","yes")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY","").strip()
SENTINEL_BASE_URL = os.getenv("SENTINEL_BASE_URL","").strip().rstrip("/")
if SENTINEL_BASE_URL and not SENTINEL_BASE_URL.startswith(("http://","https://")):
    SENTINEL_BASE_URL = "https://" + SENTINEL_BASE_URL

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("market-watcher")

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}

def _http_get(url, params=None, timeout=12, max_retry=3):
    last = None
    for i in range(max_retry):
        try:
            r = requests.get(url, params=params, headers=COMMON_HEADERS, timeout=timeout)
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

# -------- Providers --------
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
    t = yf.Ticker(symbol)  # type: ignore
    # try fast_info
    try:
        fi = getattr(t, "fast_info", None)
        if fi and getattr(fi, "last_price", None) is not None and getattr(fi, "previous_close", None) not in (None, 0):
            last = float(fi.last_price)
            prev = float(fi.previous_close)
            return (last - prev) / prev * 100.0
    except Exception:
        pass
    # fallback: 2d history
    hist = t.history(period="2d", interval="1d")
    if hist is not None and len(hist) >= 2:
        prev = float(hist["Close"].iloc[-2])
        last = float(hist["Close"].iloc[-1])
        if prev != 0:
            return (last - prev) / prev * 100.0
    raise RuntimeError("yfinance insufficient data")

def _av_change_percent(symbol: str) -> float:
    if not ALPHAVANTAGE_API_KEY:
        raise RuntimeError("ALPHAVANTAGE_API_KEY not set")
    proxies = {"^GSPC":"SPY","^IXIC":"QQQ","^VIX":"VIXY","^KS200":"069500.KS"}
    sym = proxies.get(symbol, symbol)
    url = "https://www.alphavantage.co/query"
    params = {"function":"GLOBAL_QUOTE","symbol":sym,"apikey":ALPHAVANTAGE_API_KEY}
    r = _http_get(url, params=params, timeout=12, max_retry=2)
    data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    q = data.get("Global Quote") or data.get("globalQuote") or {}
    cp = q.get("10. change percent") or q.get("changePercent")
    if cp:
        return float(str(cp).strip().rstrip("%"))
    price = q.get("05. price") or q.get("price")
    prev  = q.get("08. previous close") or q.get("previousClose")
    if price is not None and prev not in (None, "0", 0):
        price_f = float(price); prev_f = float(prev)
        if prev_f != 0:
            return (price_f - price_f) / prev_f * 100.0
    raise RuntimeError(f"alphavantage invalid quote for {sym}: {q}")

def _yahoo_change_percent(symbol: str) -> float:
    url = "https://query2.finance.yahoo.com/v7/finance/quote"
    j = _http_get(url, params={"symbols":symbol}).json()
    items = j.get("quoteResponse",{}).get("result",[])
    if not items: raise RuntimeError("yahoo empty")
    it = items[0]
    cp = it.get("regularMarketChangePercent")
    if cp is not None: return float(cp)
    price = it.get("regularMarketPrice"); prev = it.get("regularMarketPreviousClose")
    if price is not None and prev not in (None,0):
        return (float(price) - float(prev)) / float(prev) * 100.0
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

def _grade(delta: float, is_vix=False) -> str | None:
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

def _post_alert(index_name: str, delta: float | None, level: str | None, source_tag: str, note: str):
    if not SENTINEL_BASE_URL:
        raise RuntimeError("SENTINEL_BASE_URL not set")
    url = f"{SENTINEL_BASE_URL}/sentinel/alert"
    payload = {
        "index": index_name,
        "level": level if level else "CLEARED",
        "delta_pct": round(delta,2) if delta is not None else None,
        "triggered_at": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
        "note": f"{note} [{source_tag}]",
    }
    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        log.error("alert post fail %s %s", r.status_code, r.text[:200])
    else:
        log.info("알람 전송: %s %s %.2f%% (%s)", index_name, payload["level"], payload["delta_pct"] or 0.0, note)

def _is_us_market_open() -> bool:
    now = datetime.now(timezone(timedelta(hours=9)))
    m = now.month
    is_dst = 3 <= m <= 11
    h, mi = now.hour, now.minute
    if is_dst:
        return (h > 22 or (h == 22 and mi >= 30)) or (h < 5)
    else:
        return (h > 23 or (h == 23 and mi >= 30)) or (0 <= h < 6)

def run_once():
    # Decide session KST
    now = datetime.now(timezone(timedelta(hours=9)))
    sess = "KR" if (now.weekday() < 5 and (830 <= now.hour*100 + now.minute <= 1600)) else "US"
    symbols = []
    if sess == "KR":
        symbols = [("^KS200", "KR")]
    else:
        if _is_us_market_open():
            symbols = [("^GSPC", "US"),("^IXIC","US"),("^VIX","VIX")]
        else:
            symbols = [("ES=F","FUT"),("NQ=F","FUT")]  # VIX 제외

    # Collect and post
    for sym, tag in symbols:
        is_vix = (sym == "^VIX")
        try:
            delta = _chain_change_percent(sym)
            # VIX 스마트 필터(옵션): 지수변동이 작으면 VIX 알림 억제 — 간단판은 생략/보류 가능
            lvl = _grade(delta, is_vix=is_vix)
            _post_alert(sym, delta, lvl, tag, f"{'세션' if sess=='KR' else 'US'}: 레벨 판정")
        except Exception as e:
            log.warning("수집 실패 %s: %s", sym, e)

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
