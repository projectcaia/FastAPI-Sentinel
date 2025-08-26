import os, hmac, hashlib, asyncio
from starlette.requests import Request
from starlette.middleware.base import BaseHTTPMiddleware

HUB_URL = os.getenv("HUB_URL", "").strip()
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "").strip()

class HubForwardingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/sentinel/alert" and request.method.upper() == "POST":
            body_bytes = await request.body()
            response = await call_next(Request(request.scope, receive=lambda: _receive_from_bytes(body_bytes)))
            if response.status_code == 200:
                try:
                    asyncio.create_task(_forward(body_bytes))
                except Exception:
                    pass
            return response
        return await call_next(request)

async def _forward(body_bytes: bytes):
    import httpx
    try:
        sig = hmac.new(CONNECTOR_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Signature": sig}
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(HUB_URL, content=body_bytes, headers=headers)
    except Exception:
        pass

def _receive_from_bytes(data: bytes):
    done = {"sent": False}
    async def inner():
        if not done["sent"]:
            done["sent"] = True
            return {"type": "http.request", "body": data, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}
    return inner

def register_hub_forwarder(app):
    app.add_middleware(HubForwardingMiddleware)
    return app
