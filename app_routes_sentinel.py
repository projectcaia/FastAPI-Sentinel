# app_routes_sentinel.py (patched 2025-08-21)
# ------------------------------------------------------------
# Caia Relay (Assistants v2, Function Calling)
# - POST /caia/alert  : 외부(또는 내부)에서 알람 푸시 → 카이아 툴콜 강제
# - GET  /caia/health : 상태 점검
# ------------------------------------------------------------
from __future__ import annotations

import os, time, json, logging
from typing import Optional, Literal, Dict, Any, List

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

# 🔁 prefix 유지
router = APIRouter(prefix="/caia", tags=["caia-fc"])
log = logging.getLogger("uvicorn.error")

# ── ENV ──────────────────────────────────────────────────────────────
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE        = os.getenv("OPENAI_BASE", "https://api.openai.com/v1").strip()
CAIA_ASSISTANT_ID  = os.getenv("CAIA_ASSISTANT_ID", "").strip()
CAIA_THREAD_ID     = os.getenv("CAIA_THREAD_ID", "").strip()

SENTINEL_ACTIONS_BASE = os.getenv(
    "SENTINEL_ACTIONS_BASE",
    "https://fastapi-sentinel-production.up.railway.app"
).strip()

# Sentinel security header (x-sentinel-key)
SENTINEL_KEY = os.getenv("SENTINEL_KEY", "").strip()

# HTTP / Retry / Timeout
HTTP_MAX_RETRY = int(os.getenv("HTTP_MAX_RETRY", "4"))
HTTP_RETRY_WAIT_BASE = float(os.getenv("HTTP_RETRY_WAIT_BASE", "0.8"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "60"))  # 핵심: read 60s
RUN_POLL_MAX_WAIT = int(os.getenv("RUN_POLL_MAX_WAIT", "90"))

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

_DEDUP: Dict[str, float] = {}

def _dedup_key(a: AlertModel) -> str:
    return f"{a.symbol}|{a.severity}|{round(a.value, 6)}|{a.timestamp}"

def _in_window(key: str, window_min: int) -> bool:
    last = _DEDUP.get(key)
    return False if last is None else (time.time() - last) < (window_min * 60)

def _mark(key: str) -> None:
    _DEDUP[key] = time.time()

# ── HTTP helpers ─────────────────────────────────────────────────────

def _headers() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "assistants=v2"}


def _should_retry(status: int) -> bool:
    return status in (429, 500, 502, 503, 504)


def _post(url: str, body: Dict[str, Any], timeout_read: float = None) -> Dict[str, Any]:
    if timeout_read is None:
        timeout_read = READ_TIMEOUT
    last_err = None
    for i in range(HTTP_MAX_RETRY):
        try:
            r = requests.post(url, headers=_headers(), json=body, timeout=(CONNECT_TIMEOUT, timeout_read))
            if r.status_code < 300:
                return r.json()
            last_err = RuntimeError(f"POST {url} failed: {r.status_code} {r.text}")
            if _should_retry(r.status_code) and i < HTTP_MAX_RETRY-1:
                time.sleep(HTTP_RETRY_WAIT_BASE*(i+1)); continue
            raise last_err
        except Exception as e:
            last_err = e
            if i < HTTP_MAX_RETRY-1:
                time.sleep(HTTP_RETRY_WAIT_BASE*(i+1)); continue
            raise last_err


def _get(url: str, timeout_read: float = None) -> Dict[str, Any]:
    if timeout_read is None:
        timeout_read = READ_TIMEOUT
    last_err = None
    for i in range(HTTP_MAX_RETRY):
        try:
            r = requests.get(url, headers=_headers(), timeout=(CONNECT_TIMEOUT, timeout_read))
            if r.status_code < 300:
                return r.json()
            last_err = RuntimeError(f"GET {url} failed: {r.status_code} {r.text}")
            if _should_retry(r.status_code) and i < HTTP_MAX_RETRY-1:
                time.sleep(HTTP_RETRY_WAIT_BASE*(i+1)); continue
            raise last_err
        except Exception as e:
            last_err = e
            if i < HTTP_MAX_RETRY-1:
                time.sleep(HTTP_RETRY_WAIT_BASE*(i+1)); continue
            raise last_err

# ── Sentinel Actions client ──────────────────────────────────────────

def _fetch_latest_alerts(limit=10, level_min: Optional[str]=None,
                         index: Optional[str]=None, since: Optional[str]=None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": int(limit)}
    if level_min: params["level_min"] = level_min
    if index:     params["index"] = index
    if since:     params["since"] = since  # 상위에서 +09:00 ISO 보장 권장

    url = f"{SENTINEL_ACTIONS_BASE}/sentinel/inbox"

    headers: Dict[str, str] = {}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY

    try:
        r = requests.get(url, headers=headers, params=params, timeout=(CONNECT_TIMEOUT, 30))
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or data.get("items") is None:
            data = {"items": [{
                "index":"SYSTEM","level":"LV1","delta_pct":0,
                "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                "note":"최근 알림 없음 (fallback)"
            }]}
        return data
    except Exception as e:
        log.error("[Caia-FC] _fetch_latest_alerts failed: %s", e)
        return {"items": [{
            "index":"SYSTEM","level":"LV1","delta_pct":0,
            "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            "note": f"Actions 호출 실패: {e}"
        }]}

# ── Assistants v2 Run orchestration ─────────────────────────────────

def _create_run_get_latest() -> str:
    tools_def = [{
        "type": "function",
        "function": {
            "name": "getLatestAlerts",
            "description": "최근 센티넬 알림 조회",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":     {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "level_min": {"type": "string", "enum": ["LV1","LV2","LV3"]},
                    "index":     {"type": "string"},
                    "since":     {"type": "string"}  # ISO8601(+09:00) 권장
                }
            }
        }
    }]

    run = _post(f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/runs", {
        "assistant_id": CAIA_ASSISTANT_ID,
        "tools": tools_def,  # ← 인라인 툴 스키마 주입
        "tool_choice": {"type":"function","function":{"name":"getLatestAlerts"}},
        "instructions": (
            "센티넬/알람 키워드 감지. 규칙에 따라 getLatestAlerts를 호출해 최근 알림을 요약하라. "
            "요약만 하고 전략 판단은 묻기 전 금지. 마지막에 '전략 판단 들어갈까요?'를 붙여라."
        )
    })
    return run["id"]


def _relay_to_caia_with_tool(push_payload: Dict[str, Any]) -> Dict[str, Any]:
    if not CAIA_ASSISTANT_ID or not CAIA_THREAD_ID:
        raise RuntimeError("CAIA_ASSISTANT_ID or CAIA_THREAD_ID is missing")

    _post(f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/messages", {
        "role":"user",
        "content":[{"type":"text","text":
            "[Sentinel] 알람 수신\n"
            f"payload: {json.dumps(push_payload, ensure_ascii=False)}\n"
            "지침: 최신 알림만 요약, (지표, 레벨, Δ%, 시각, note) 리스트. "
            "전략 판단은 묻기 전 금지. 마지막에 '전략 판단 들어갈까요?' 추가."
        }]
    })

    run_id = _create_run_get_latest()
    replayed = False
    start = time.time()

    while True:
        cur = _get(f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/runs/{run_id}")
        st = cur.get("status")

        if st in ("queued","in_progress","cancelling"):
            if time.time() - start > RUN_POLL_MAX_WAIT:
                return {"status":"timeout","id": run_id}
            time.sleep(0.8); continue

        if st == "requires_action":
            ra = cur.get("required_action", {}).get("submit_tool_outputs", {})
            calls: List[Dict[str, Any]] = ra.get("tool_calls", []) or []
            outputs: List[Dict[str, str]] = []

            for call in calls:
                fn   = call.get("function", {}).get("name")
                args = call.get("function", {}).get("arguments") or "{}"
                try: parsed = json.loads(args)
                except Exception: parsed = {}

                if fn == "getLatestAlerts":
                    data = _fetch_latest_alerts(
                        limit=int(parsed.get("limit", 10)),
                        level_min=parsed.get("level_min"),
                        index=parsed.get("index"),
                        since=parsed.get("since"),
                    )
                    outputs.append({"tool_call_id": call["id"],
                                    "output": json.dumps(data, ensure_ascii=False)})
                else:
                    outputs.append({"tool_call_id": call["id"],
                                    "output": json.dumps({"error": f"unknown function {fn}"}, ensure_ascii=False)})

            _post(f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/runs/{run_id}/submit_tool_outputs",
                  {"tool_outputs": outputs})
            time.sleep(0.6); continue

        if st == "failed" and (os.getenv("RUN_ALLOW_REPLAY", "1").strip() not in ("0","false","False")) and not replayed:
            log.warning("[Caia-FC] run failed -> replay once")
            run_id = _create_run_get_latest()
            start = time.time()
            replayed = True
            time.sleep(0.8); continue

        return cur  # completed / failed / cancelled / timeout

# ── Routes ──────────────────────────────────────────────────────────

@router.post("/alert")
def caia_alert_push(alert: AlertModel):
    try:
        window_min = int(os.getenv("DEDUP_WINDOW_MIN", "0"))
        key = _dedup_key(alert)
        if window_min > 0 and _in_window(key, window_min):
            log.info("[Caia-FC] dedup suppressed: %s", key)
            return {"status":"ok","dedup":True}

        log.info("[Caia-FC] Alert recv: %s", alert.model_dump())
        res = _relay_to_caia_with_tool(alert.model_dump())
        _mark(key)
        return {"status":"ok","run_status": res.get("status","unknown"), "run_id": res.get("id")}
    except Exception as e:
        log.exception("[Caia-FC] relay error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
def caia_health():
    return {"status":"ok",
            "assistant_id": bool(CAIA_ASSISTANT_ID),
            "thread_id": bool(CAIA_THREAD_ID),
            "openai_base": OPENAI_BASE,
            "sentinel_actions_base": SENTINEL_ACTIONS_BASE,
            "timeouts": {"connect": CONNECT_TIMEOUT, "read": READ_TIMEOUT},
            "retries": {"max": HTTP_MAX_RETRY, "backoff_base": HTTP_RETRY_WAIT_BASE},
            "run_poll_max_wait": RUN_POLL_MAX_WAIT}
