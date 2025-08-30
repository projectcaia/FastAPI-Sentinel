from fastapi import APIRouter, Request, HTTPException
import os, json, httpx
from typing import Optional
from .utils import is_market_open
from .security import verify_hmac, compute_hmac_sha256

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
                resp = await client.post(url, content=body_bytes, headers=headers)
                last_text = resp.text
                if resp.status_code < 500:
                    return resp.status_code, last_text
            except Exception as e:
                last_text = str(e)
        return 599, last_text

@router.post("/sentinel/alert")
async def sentinel_alert(request: Request):
    raw = await request.body()

    # Optional inbound HMAC check: if header provided, verify against CONNECTOR_SECRET
    sig = request.headers.get("X-Signature")
    if sig and CONNECTOR_SECRET and not verify_hmac(raw, CONNECTOR_SECRET, sig):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Parse JSON (optional)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        data = None

    # Market closed suppression
    if not is_market_open():
        # return 200 so upstream does not retry
        return {"ok": True, "skipped": "market_closed", "detail": "Weekend/holiday in Asia/Seoul"}

    # If HUB_URL configured, forward with HMAC + Idempotency-Key
    if HUB_URL:
        # compute HMAC over the raw body for the *forward* request
        forward_sig = compute_hmac_sha256(raw, CONNECTOR_SECRET) if CONNECTOR_SECRET else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if forward_sig:
            headers["X-Signature"] = forward_sig
        # Idempotency: use provided key or derive from body
        idemp = None
        if isinstance(data, dict):
            idemp = data.get("idempotency_key")
        if not idemp:
            import hashlib as _hashlib
            idemp = _hashlib.sha256(raw).hexdigest()[:32]
        headers["Idempotency-Key"] = idemp

        status, text = await _post_with_retries(HUB_URL, raw, headers, max_retries=5)
        if status != 200:
            raise HTTPException(status_code=status, detail=f"hub forward failed: {text}")
        return {"ok": True, "forwarded": True, "status": status, "resp": text}

    # Otherwise just acknowledge locally
    return {"ok": True, "forwarded": False}
