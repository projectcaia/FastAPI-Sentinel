from fastapi import APIRouter, Request, HTTPException
import os, hmac, hashlib, httpx

router = APIRouter()
HUB_URL = os.getenv("HUB_URL", "").strip()
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "").strip()

@router.post("/sentinel/alert")
async def sentinel_alert(request: Request):
    if not HUB_URL or not CONNECTOR_SECRET:
        raise HTTPException(500, detail="HUB_URL/CONNECTOR_SECRET not set")
    raw = await request.body()
    sig = hmac.new(CONNECTOR_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Signature": sig}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(HUB_URL, content=raw, headers=headers)
        return {"ok": r.status_code == 200, "status": r.status_code}
