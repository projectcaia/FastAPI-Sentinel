# app/routes/sentinel.py
# ------------------------------------------------------------
# Sentinel → Caia Relay Router
# - POST /sentinel/alert : receive an alert and relay it to Caia main chat
# - Minimal, drop-in patch: add this router and include_router(...) in main.py
# ------------------------------------------------------------
from __future__ import annotations

import os
import time
import json
import logging
from typing import Optional, Literal, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from openai import AsyncOpenAI

router = APIRouter(prefix="/sentinel", tags=["sentinel"])
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
# Relay → Caia
# -----------------------
TOOL_NAME = "sentinel.alert"

def _format_tool_call(tool_name: str, payload: Dict[str, Any]) -> str:
    # Caia 훅이 이 패턴을 감지해 FunctionCalling처럼 처리하도록 합의된 포맷
    return f'call_tool("{tool_name}", {json.dumps(payload, ensure_ascii=False)})'

async def relay_to_caia(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strategy A (기본): Assistants Threads 사용
      - 필요 ENV: OPENAI_API_KEY, CAIA_THREAD_ID, CAIA_ASSISTANT_ID
      - 동작: thread에 user 메시지로 call_tool(...) 주입 → run 생성
    Fallback B: Webhook (선택)
      - 필요 ENV: CAIA_HOOK_URL (POST JSON 그대로)
    """
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    thread_id = os.getenv("CAIA_THREAD_ID")
    asst_id = os.getenv("CAIA_ASSISTANT_ID")  # 권장
    hook_url = os.getenv("CAIA_HOOK_URL")     # 선택

    if thread_id and asst_id:
        # Strategy A: Assistants Threads
        content = _format_tool_call(TOOL_NAME, payload)
        meta = {"origin": "sentinel", "tool": TOOL_NAME}

        # 1) 메시지 주입
        msg = await client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=content,
            metadata=meta,
        )
        # 2) 실행 트리거
        run = await client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=asst_id,
            instructions="Process the tool-call message and update the main chat.",
        )
        return {"strategy": "assistants", "message_id": msg.id, "run_id": run.id}

    # Fallback B: Raw webhook (선택)
    if hook_url:
        import httpx
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(hook_url, json={"tool": TOOL_NAME, "payload": payload})
            r.raise_for_status()
            return {"strategy": "webhook", "status": r.status_code}

    raise RuntimeError(
        "Relay configuration missing. Set CAIA_THREAD_ID and CAIA_ASSISTANT_ID or CAIA_HOOK_URL."
    )

# -----------------------
# Routes
# -----------------------
@router.post("/alert")
async def sentinel_alert(alert: AlertModel):
    try:
        # Dedup (optional)
        window_min = int(os.getenv("DEDUP_WINDOW_MIN", "0"))
        key = _dedup_key(alert)
        if window_min > 0 and _in_window(key, window_min):
            log.info(f"[Sentinel] dedup suppressed: {key}")
            return {"status": "ok", "dedup": True}

        log.info(f"[Sentinel] Alert recv: {alert.model_dump()}")
        result = await relay_to_caia(alert.model_dump())
        _mark(key)
        return {"status": "ok", "caia_result": result}

    except Exception as e:
        log.exception("relay error")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
async def health():
    # Quick health for readiness/liveness probes
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "thread": bool(os.getenv("CAIA_THREAD_ID")),
        "assistant": bool(os.getenv("CAIA_ASSISTANT_ID")),
        "hook": bool(os.getenv("CAIA_HOOK_URL")),
    }
