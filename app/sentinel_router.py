from fastapi import APIRouter, Request, HTTPException
import os, hmac, hashlib, httpx, asyncio, json, hashlib as _hashlib
from typing import Optional

router = APIRouter()
HUB_URL = os.getenv("HUB_URL", "").strip()
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "").strip()

async def _post_with_retries(url: str, body_bytes: bytes, headers: dict, max_retries: int = 5) -> tuple[int, Optional[str]]:
    attempts = 0
    last_text = None
    timeout = httpx.Timeout(10.0, connect=10.0, read=10.0, write=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        while attempts < max_retries:
            attempts += 1
            try:
                r = await client.post(url, content=body_bytes, headers=headers)
                if r.status_code == 200:
                    return r.status_code, r.text
                last_text = r.text
                if r.status_code not in (429, 500, 502, 503, 504):
                    # do not retry for client errors
                    return r.status_code, last_text
            except Exception as e:
                last_text = str(e)
            # backoff
            await asyncio.sleep(0.5 * (2 ** (attempts - 1)))
        return 0, last_text

@router.post("/sentinel/alert")
async def sentinel_alert(request: Request):
    if not HUB_URL or not CONNECTOR_SECRET:
        raise HTTPException(500, detail="HUB_URL/CONNECTOR_SECRET not set")
    raw = await request.body()
    # Normalize as UTF-8 JSON to avoid ascii encode errors
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        # if it's not JSON, forward raw bytes anyway
        data = None
    if data is not None:
        body_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    else:
        body_bytes = raw  # pass-through

    # Compute signature over the exact bytes we will send
    sig = hmac.new(CONNECTOR_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    # Provide idempotency key if caller did not set one
    idemp = None
    if isinstance(data, dict):
        idemp = data.get("idempotency_key")
        if not idemp:
            # derive a stable key from payload content
            idemp = _hashlib.sha256(body_bytes).hexdigest()[:32]
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Signature": sig,
    }
    if idemp:
        headers["Idempotency-Key"] = idemp

    status, text = await _post_with_retries(HUB_URL, body_bytes, headers, max_retries=5)
    return {"ok": status == 200, "status": status, "resp": text}
