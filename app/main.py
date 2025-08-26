from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import os
from .function_router import router as function_router

# Optional hub forwarder (safe if missing)
try:
    from .hub_patch import register_hub_forwarder  # expects app/hub_patch.py
except Exception:  # hub_patch not present or import error
    register_hub_forwarder = None

APP_VERSION = os.environ.get("APP_VERSION", "2025-08-25.v2.4")
DEBUG_ROUTES = os.environ.get("DEBUG_ROUTES", "0") == "1"

app = FastAPI(title="Caia Connector & Memory Rules v2.4")

# Register Hub forwarder middleware (non-fatal if unavailable)
if register_hub_forwarder is not None:
    try:
        register_hub_forwarder(app)
    except Exception:
        # do not crash app if hub patch misconfigured
        pass

@app.get("/ready")
async def ready():
    details = {}
    try:
        import importlib
        try:
            importlib.import_module("schedule")  # optional
            details["schedule"] = "ok"
        except Exception as e:
            details["schedule"] = f"missing_or_error: {type(e).__name__}"
    except Exception as e:
        details["int"] = f"error: {type(e).__name__}"
    return {"status": "ready", "version": APP_VERSION, "scheduler_started": False, "details": details}

@app.get("/__routes")
async def __routes():
    if not DEBUG_ROUTES:
        return JSONResponse(status_code=404, content={"error": "disabled"})
    return {"routes": [r.path for r in app.router.routes]}

# Mount routers
app.include_router(function_router)

# Global error handler (don't leak stack in health)
@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"ok": False, "error": type(exc).__name__, "detail": str(exc)})
