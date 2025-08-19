# app_routes_sentinel.py
# ------------------------------------------------------------
# Sentinel → Caia Relay Router (SDK-free, uses OpenAI REST via requests)
# Exposes (when included with prefix="/fc"):
#   POST /fc/sentinel/alert
#   GET  /fc/sentinel/health
# ------------------------------------------------------------
from __future__ import annotations

import os
import time
import json
import logging
from typing import Optional, Literal, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import requests

router = APIRouter(prefix="/sentinel", tags=["sentinel-fc"])
log = logging.getLogger("uvicorn.error")

# -----------------------
# Pydantic schema
# -----------------------
class AlertModel(BaseModel):
    symbol: str = Field(..., description="지표명 (ΔK200, COVIX, VIX 등)")
    value: float = Field(..., description="지표 값")
    severity: Literal["INFO", "WARN", "CRIT"] = Field(..., description="알람 등급")
    timestamp: str = Field(..., description="ISO8601(+09:00) 권장")
    message: Optional[str] = Field(None, description="알람 상세 설명")

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        return v.strip()

# -----------------------
# Simple in-memory dedup
# -----------------------
_DEDUP: Dict[str, float] = {}

def _dedup_key(a: AlertModel) -> str:
    return f"{a.symbol}|{a.severity}|{round(a.value, 6)}|{a.timestamp}"

def _in_window(key: str, window_min: int) -> bool:
    last = _DEDUP.get(key)
    if last is None:
        return False
    return (time.time() - last) < (window_min * 60)

def _mark(key: str) -> None:
    _DEDUP[key] = time.time()

# -----------------------
# Relay → Caia (Assistants v2 REST)
# -----------------------
TOOL_NAME = "sentinel.alert"

def _format_tool_call(tool_name: str, payload: Dict[str, Any]) -> str:
    # Caia 훅이 이 패턴을 감지해 FunctionCalling처럼 처리하도록 합의된 포맷
    return f'call_tool("{tool_name}", {json.dumps(payload, ensure_ascii=False)})'

def _assistants_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }

def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int = 15):
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    return r

def relay_to_caia(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strategy A: Assistants Threads (권장)
      - ENV:
          OPENAI_API_KEY
          CAIA_ASSISTANT_ID
          CAIA_THREAD_ID
    Fallback B: Raw webhook (선택)
      - ENV:
          CAIA_HOOK_URL
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    asst_id = os.getenv("CAIA_ASSISTANT_ID", "").strip()
    thread_id = os.getenv("CAIA_THREAD_ID", "").strip()
    hook_url = os.getenv("CAIA_HOOK_URL", "").strip()

    if api_key and asst_id and thread_id:
        base = "https://api.openai.com/v1"
        headers = _assistants_headers(api_key)
        content = _format_tool_call(TOOL_NAME, payload)
        meta = {"origin": "sentinel", "tool": TOOL_NAME}

        # 1) 메시지 주입
        r1 = _post_json(
            f"{base}/threads/{thread_id}/messages",
            headers,
            {"role": "user", "content": content, "metadata": meta},
            timeout=12,
        )
        if r1.status_code == 404:
            raise RuntimeError("CAIA_THREAD_ID not found (404).")
        if not r1.ok:
            raise RuntimeError(f"Message add failed: {r1.status_code} {r1.text}")

        # 2) Run 실행
        r2 = _post_json(
            f"{base}/threads/{thread_id}/runs",
            headers,
            {"assistant_id": asst_id},
            timeout=12,
        )
        if not r2.ok:
            raise RuntimeError(f"Run create failed: {r2.status_code} {r2.text}")

        return {
            "strategy": "assistants",
            "message_id": (r1.json().get("id") if r1.headers.get("content-type","").startswith("application/json") else None),
            "run_id": (r2.json().get("id") if r2.headers.get("content-type","").startswith("application/json") else None),
        }

    # Fallback B: Raw webhook
    if hook_url:
        r = requests.post(hook_url, json={"tool": TOOL_NAME, "payload": payload}, timeout=10)
        r.raise_for_status()
        return {"strategy": "webhook", "status": r.status_code}

    raise RuntimeError(
        "Relay configuration missing. Set OPENAI_API_KEY, CAIA_ASSISTANT_ID, CAIA_THREAD_ID or CAIA_HOOK_URL."
    )

# -----------------------
# Routes
# -----------------------
@router.post("/alert")
def fc_sentinel_alert(alert: AlertModel):
    try:
        window_min = int(os.getenv("DEDUP_WINDOW_MIN", "0"))
        key = _dedup_key(alert)
        if window_min > 0 and _in_window(key, window_min):
            log.info(f"[Sentinel-FC] dedup suppressed: {key}")
            return {"status": "ok", "dedup": True}

        log.info(f"[Sentinel-FC] Alert recv: {alert.model_dump()}")
        result = relay_to_caia(alert.model_dump())
        _mark(key)
        return {"status": "ok", "caia_result": result}

    except Exception as e:
        log.exception("relay error")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
def fc_health():
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "thread": bool(os.getenv("CAIA_THREAD_ID")),
        "assistant": bool(os.getenv("CAIA_ASSISTANT_ID")),
        "hook": bool(os.getenv("CAIA_HOOK_URL")),
    }
