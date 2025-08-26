from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import os


# Try both import styles so it works whether /app is a package or not
try:
from .function_router import router as function_router # type: ignore
except Exception:
try:
from function_router import router as function_router # type: ignore
except Exception:
function_router = None


# Optional hub forwarder (safe if hub_patch.py missing)
try:
import hub_patch
register_hub_forwarder = getattr(hub_patch, "register_hub_forwarder", None)
except Exception:
register_hub_forwarder = None


APP_VERSION = os.environ.get("APP_VERSION", "2025-08-26.v2.5")


app = FastAPI(title="Sentinel Service")


if register_hub_forwarder is not None:
try:
register_hub_forwarder(app)
except Exception:
pass


@app.get("/ready")
async def ready():
return {"status": "ready", "version": APP_VERSION}


@app.get("/__routes")
async def __routes():
return {"routes": [r.path for r in app.router.routes]}


if function_router is not None:
app.include_router(function_router)


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
return JSONResponse(status_code=500, content={"ok": False, "error": type(exc).__name__, "detail": str(exc)})


# ⚠️ DO NOT add any non‑ASCII / Korean text in this file.
# ⚠️ Keep exactly these lines; do not insert prose between imports and code.
