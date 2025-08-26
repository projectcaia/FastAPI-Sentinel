
"""
hub_patch.py — Minimal ASGI middleware to forward /sentinel/alert payloads to Connector Hub.

Usage (add 2 lines near FastAPI app creation):
    from hub_patch import register_hub_forwarder
    register_hub_forwarder(app)

Env required:
    HUB_URL=https://<HUB_DOMAIN>.up.railway.app/bridge/ingest
    CONNECTOR_SECRET=<shared_secret>
"""
import os, hmac, hashlib, json, asyncio
from typing import Callable
from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import datetime, timezone

HUB_URL = os.getenv("HUB_URL", "").strip()
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "").strip()

def _make_sig(raw: bytes) -> str:
    if not CONNECTOR_SECRET:
        return ""
    return hmac.new(CONNECTOR_SECRET.encode(), raw, hashlib.sha256).hexdigest()

def _ensure_idempotency_key(body: dict) -> str:
    if "idempotency_key" in body and body["idempotency_key"]:
        return body["idempotency_key"]
    # fallback: deterministic key from type + ts
    ts = body.get("timestamp") or datetime.now(timezone.utc).isoformat()
    seed = (body.get("type","alert") + "|" + ts).encode()
    h = hashlib.sha256(seed).hexdigest()[:8]
    return f"SN-{ts.replace(':','').replace('-','').replace('.','')}-{h}"

async def _forward_to_hub(body: dict):
    if not HUB_URL or not CONNECTOR_SECRET:
        return
    try:
        import httpx
    except Exception:
        # httpx가 없으면 skip (센티넬 본 기능에는 영향 없음)
        return
    idemp = _ensure_idempotency_key(body)
    raw = json.dumps({
        "idempotency_key": idemp,
        "source": body.get("source","sentinel"),
        "type": body.get("type","alert.market"),
        "priority": body.get("priority","medium"),
        "timestamp": body.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "payload": body.get("payload") or body,
    }, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Signature": _make_sig(raw),
        "Idempotency-Key": idemp,
    }
    # 비동기 백그라운드 전송 (본 요청 경로와 분리)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(HUB_URL, content=raw, headers=headers)
        except Exception:
            pass  # 실패해도 센티넬 경로에는 영향 주지 않음

class _HubForwardingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        if request.url.path == "/sentinel/alert" and request.method.upper() == "POST":
            # body를 미리 읽고 downstream에도 전달 가능하도록 stream 재생성
            body_bytes = await request.body()
            try:
                body = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                body = {}

            # downstream 실행 (텔레그램 등 기존 기능 우선)
            response = await call_next(Request(request.scope, receive=lambda: _receive_from_bytes(body_bytes)))
            # 200인 경우에만 Hub로 포워딩 (백그라운드)
            if response.status_code == 200:
                asyncio.create_task(_forward_to_hub(body))
            return response
        else:
            return await call_next(request)

def _receive_from_bytes(data: bytes):
    # Starlette가 기대하는 receive 콜러블을 만들어준다
    done = {"sent": False}
    async def inner():
        if not done["sent"]:
            done["sent"] = True
            return {"type": "http.request", "body": data, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}
    return inner

def register_hub_forwarder(app):
    \"\"\"Call this with your FastAPI app instance to enable forwarding.\"\"\"
    app.add_middleware(_HubForwardingMiddleware)
    return app
