#!/usr/bin/env python3
import os, time, json, logging, math, hmac, hashlib, sys
from typing import Optional, Dict, Any

YF_ENABLED = os.getenv("YF_ENABLED", "true").lower() in ("1","true","yes")
HUB_URL = os.getenv("HUB_URL") or (os.getenv("BASE_URL", "http://localhost:8080").rstrip("/") + "/bridge/ingest")
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "1800"))

LOG_LEVEL = os.getenv("WATCHER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(levelname)s:market-watcher:%(message)s")
log = logging.getLogger("market-watcher")

def hmac_sig(body: str) -> str:
    return hmac.new(CONNECTOR_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()

def http_post(url: str, data: str, headers: Dict[str,str]) -> tuple[int, str]:
    import requests
    r = requests.post(url, data=data, headers=headers, timeout=15)
    return r.status_code, r.text

def yahoo_rest_quote(symbol: str) -> Optional[float]:
    import requests
    url = f"https://query2.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    hdr = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://finance.yahoo.com/",
        "Connection": "close",
    }
    r = requests.get(url, headers=hdr, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    res = j.get("quoteResponse", {}).get("result", [])
    if not res:
        return None
    price = res[0].get("regularMarketPrice") or res[0].get("postMarketPrice") or res[0].get("preMarketPrice")
    return float(price) if price is not None else None

def yf_quote(symbol: str) -> Optional[float]:
    import yfinance as yf
    t = yf.Ticker(symbol)
    try:
        info = t.fast_info
        price = getattr(info, "last_price", None) or (info.get("lastPrice") if hasattr(info, "get") else None)
        if not price:
            # fallback to .info (slower)
            price = t.info.get("regularMarketPrice")
        return float(price) if price is not None else None
    except Exception as e:
        raise

def get_price(symbol: str) -> Optional[float]:
    if YF_ENABLED:
        try:
            return yf_quote(symbol)
        except Exception as e:
            log.warning(f"yfinance 실패({symbol}): {e}")
    try:
        return yahoo_rest_quote(symbol)
    except Exception as e:
        log.warning(f"Yahoo REST 실패({symbol}): {e}")
        return None

def compute_pct(cur: Optional[float], base: Optional[float]) -> float:
    if cur is None or base is None or base == 0:
        return 0.0
    return (cur / base - 1.0) * 100.0

def decide_level(pct: float) -> str:
    ab = abs(pct)
    if ab >= 2.5: return "LV3"
    if ab >= 1.5: return "LV2"
    if ab >= 0.8: return "LV1"
    return "LV0"

def build_payload(index: str, rule: str, level: str, dK200: float, dVIX: float) -> dict:
    # timestamp in local with offset if available
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return {
        "idempotency_key": f"MW-{int(time.time())}-{index}",
        "source": "sentinel",
        "type": "alert.market",
        "priority": "high" if level in ("LV2","LV3") else "normal",
        "timestamp": ts,
        "payload": {
            "rule": rule,
            "index": index,
            "level": level,
            "metrics": {"dK200": round(dK200, 2), "dVIX": round(dVIX, 2)}
        }
    }

def push(body: dict) -> None:
    raw = json.dumps(body, ensure_ascii=False)
    sig = hmac_sig(raw)
    headers = {
        "Content-Type": "application/json",
        "X-Signature": sig,
        "Idempotency-Key": body["idempotency_key"],
    }
    code, text = http_post(HUB_URL, raw, headers)
    if code != 200:
        raise RuntimeError(f"push fail {code}: {text[:200]}")
    log.info(f"허브 전송 완료: {body['idempotency_key']}")

def run_once():
    # Symbols: ES=F (S&P500 fut), NQ=F (NASDAQ fut), ^VIX
    es = get_price("ES=F")
    nq = get_price("NQ=F")
    vix = get_price("^VIX")

    # Placeholder deltas (snapshot → 0.0). Replace with your own baseline if needed.
    k200_pct = 0.0
    vix_pct = 0.0

    level = decide_level(k200_pct)
    body = build_payload(index="KOSPI200", rule="iv_spike", level=level, dK200=k200_pct, dVIX=vix_pct)
    push(body)

def main():
    log.info(f"시장감시 작업 시작: 주기={POLL_SECONDS}s, base={HUB_URL}")
    if not CONNECTOR_SECRET:
        log.error("CONNECTOR_SECRET 누락 — 종료")
        sys.exit(2)
    backoff = 5
    while True:
        try:
            run_once()
            time.sleep(POLL_SECONDS)
            backoff = 5
        except Exception as e:
            log.warning(f"수집/전송 실패: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 600)

if __name__ == "__main__":
    main()
