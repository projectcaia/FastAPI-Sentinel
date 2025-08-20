# app_routes_sentinel.py
# ------------------------------------------------------------
# Sentinel → Caia Relay (Assistants v2, Function Calling 정식 처리)
# - POST /sentinel/alert  : 센티넬 서버가 알람 푸시
# - GET  /sentinel/health : 상태 점검
# ------------------------------------------------------------
from __future__ import annotations

import os
import time
import json
import logging
from typing import Optional, Literal, Dict, Any, List

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/sentinel", tags=["sentinel-fc"])
log = logging.getLogger("uvicorn.error")

# =========================
# 환경변수
# =========================
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE        = os.getenv("OPENAI_BASE", "https://api.openai.com/v1").strip()
CAIA_ASSISTANT_ID  = os.getenv("CAIA_ASSISTANT_ID", "").strip()
CAIA_THREAD_ID     = os.getenv("CAIA_THREAD_ID", "").strip()

# Sentinel Actions (OpenAPI 스키마 서버)
SENTINEL_ACTIONS_BASE = os.getenv(
    "SENTINEL_ACTIONS_BASE",
    "https://fastapi-sentinel-production.up.railway.app"
).strip()

# 재시도 설정 (안정화)
HTTP_MAX_RETRY = int(os.getenv("HTTP_MAX_RETRY", "3"))        # OpenAI/Actions 호출 재시도 횟수
HTTP_RETRY_WAIT_BASE = float(os.getenv("HTTP_RETRY_WAIT_BASE", "0.6"))  # backoff 기본

# Run 재생성 1회 허용(모델 일시 오류 방지)
RUN_ALLOW_REPLAY = os.getenv("RUN_ALLOW_REPLAY", "1").strip() not in ("0", "false", "False")

# =========================
# 모델 / 스키마
# =========================
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

# =========================
# 간단 중복 억제 (옵션)
# =========================
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

# =========================
# HTTP helpers (재시도 내장)
# =========================
def _headers() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }

def _should_retry(status_code: int) -> bool:
    return status_code in (429, 500, 502, 503, 504)

def _post(url: str, body: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    last_err = None
    for i in range(HTTP_MAX_RETRY):
        try:
            r = requests.post(url, headers=_headers(), json=body, timeout=timeout)
            if r.status_code < 300:
                return r.json()
            last_err = RuntimeError(f"POST {url} failed: {r.status_code} {r.text}")
            if _should_retry(r.status_code) and i < HTTP_MAX_RETRY - 1:
                time.sleep(HTTP_RETRY_WAIT_BASE * (i + 1))
                continue
            raise last_err
        except Exception as e:
            last_err = e
            if i < HTTP_MAX_RETRY - 1:
                time.sleep(HTTP_RETRY_WAIT_BASE * (i + 1))
                continue
            raise last_err

def _get(url: str, timeout: int = 15) -> Dict[str, Any]:
    last_err = None
    for i in range(HTTP_MAX_RETRY):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout)
            if r.status_code < 300:
                return r.json()
            last_err = RuntimeError(f"GET {url} failed: {r.status_code} {r.text}")
            if _should_retry(r.status_code) and i < HTTP_MAX_RETRY - 1:
                time.sleep(HTTP_RETRY_WAIT_BASE * (i + 1))
                continue
            raise last_err
        except Exception as e:
            last_err = e
            if i < HTTP_MAX_RETRY - 1:
                time.sleep(HTTP_RETRY_WAIT_BASE * (i + 1))
                continue
            raise last_err

# =========================
# Sentinel Actions 호출
# =========================
def _fetch_latest_alerts(
    limit: int = 10,
    level_min: Optional[str] = None,
    index: Optional[str] = None,
    since: Optional[str] = None
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": int(limit)}
    if level_min:
        params["level_min"] = level_min
    if index:
        params["index"] = index
    if since:
        params["since"] = since
    try:
        r = requests.get(
            f"{SENTINEL_ACTIONS_BASE}/sentinel/inbox",
            params=params,
            timeout=8
        )
        r.raise_for_status()
        data = r.json()
        # 결과가 완전 비면 모델이 멈추는 걸 방지하는 최소 더미
        if not isinstance(data, dict) or not data.get("items"):
            data = {
                "items": [{
                    "index": "SYSTEM",
                    "level": "LV1",
                    "delta_pct": 0,
                    "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                    "note": "최근 알림 없음 (fallback)"
                }]
            }
        return data
    except Exception as e:
        log.error("[Sentinel-FC] _fetch_latest_alerts failed: %s", e)
        return {
            "items": [{
                "index": "SYSTEM",
                "level": "LV1",
                "delta_pct": 0,
                "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                "note": f"Actions 호출 실패: {e}"
            }]
        }

# =========================
# 핵심: Function Calling 정식 루프
# =========================
def _create_run_get_latest() -> str:
    """getLatestAlerts 툴콜을 강제하는 run 생성 후 run_id 반환"""
    run = _post(
        f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/runs",
        {
            "assistant_id": CAIA_ASSISTANT_ID,
            "instructions": (
                "센티넬/알람 키워드 감지. 규칙에 따라 getLatestAlerts를 호출해 최근 알림을 요약하라. "
                "요약만 하고 전략 판단은 묻기 전 금지. 마지막에 '전략 판단 들어갈까요?'를 붙여라."
            ),
            "tool_choice": {
                "type": "function",
                "function": {"name": "getLatestAlerts"}  # operationId와 정확히 일치
            }
        }
    )
    return run["id"]

def _relay_to_caia_with_tool(push_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    1) Thread에 사용자 메시지 추가 (컨텍스트 제공)
    2) run 생성 시 tool_choice로 getLatestAlerts 강제 호출
    3) requires_action 수신 시 → 우리 서버가 Sentinel Actions를 직접 호출해 결과를 submit_tool_outputs
    4) run 완료/실패 상태를 반환
    """
    if not CAIA_ASSISTANT_ID or not CAIA_THREAD_ID:
        raise RuntimeError("CAIA_ASSISTANT_ID or CAIA_THREAD_ID is missing")

    # 1) 컨텍스트 메시지(유저 역할) 추가
    _post(
        f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/messages",
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[Sentinel] 알람 수신\n"
                        f"payload: {json.dumps(push_payload, ensure_ascii=False)}\n"
                        "지침: 최신 알림만 요약, (지표, 레벨, Δ%, 시각, note) 리스트. "
                        "전략 판단은 묻기 전 금지. 마지막에 '전략 판단 들어갈까요?' 추가."
                    )
                }
            ]
        }
    )

    # 2) run 생성 (필요 시 1회 재생성 허용)
    run_id = _create_run_get_latest()
    replayed = False

    # 3) requires_action 처리 루프
    while True:
        cur = _get(f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/runs/{run_id}")
        st = cur.get("status")

        if st in ("queued", "in_progress", "cancelling"):
            time.sleep(0.7)
            continue

        if st == "requires_action":
            ra = cur.get("required_action", {}).get("submit_tool_outputs", {})
            calls: List[Dict[str, Any]] = ra.get("tool_calls", []) or []
            outputs: List[Dict[str, str]] = []

            for call in calls:
                fn = call.get("function", {}).get("name")
                args = call.get("function", {}).get("arguments") or "{}"
                try:
                    parsed = json.loads(args)
                except Exception:
                    parsed = {}

                if fn == "getLatestAlerts":
                    data = _fetch_latest_alerts(
                        limit     = int(parsed.get("limit", 10)),
                        level_min = parsed.get("level_min"),
                        index     = parsed.get("index"),
                        since     = parsed.get("since"),
                    )
                    outputs.append({
                        "tool_call_id": call["id"],
                        "output": json.dumps(data, ensure_ascii=False)
                    })
                else:
                    outputs.append({
                        "tool_call_id": call["id"],
                        "output": json.dumps({"error": f"unknown function {fn}"}, ensure_ascii=False)
                    })

            _post(
                f"{OPENAI_BASE}/threads/{CAIA_THREAD_ID}/runs/{run_id}/submit_tool_outputs",
                {"tool_outputs": outputs}
            )
            # 다음 루프에서 상태 재확인
            time.sleep(0.4)
            continue

        if st == "failed" and RUN_ALLOW_REPLAY and not replayed:
            # 모델 일시 오류/툴콜 누락 방지: 동일 조건으로 1회 재시도
            log.warning("[Sentinel-FC] run failed -> replay once")
            run_id = _create_run_get_latest()
            replayed = True
            time.sleep(0.6)
            continue

        # completed / failed / cancelled
        return cur

# =========================
# 라우트
# =========================
@router.post("/alert")
def sentinel_alert_push(alert: AlertModel):
    try:
        # (옵션) 중복 억제
        window_min = int(os.getenv("DEDUP_WINDOW_MIN", "0"))
        key = _dedup_key(alert)
        if window_min > 0 and _in_window(key, window_min):
            log.info("[Sentinel-FC] dedup suppressed: %s", key)
            return {"status": "ok", "dedup": True}

        log.info("[Sentinel-FC] Alert recv: %s", alert.model_dump())
        res = _relay_to_caia_with_tool(alert.model_dump())
        _mark(key)
        return {
            "status": "ok",
            "run_status": res.get("status", "unknown"),
            "run_id": res.get("id")
        }

    except Exception as e:
        log.exception("[Sentinel-FC] relay error")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
def sentinel_health():
    return {
        "status": "ok",
        "assistant_id": bool(CAIA_ASSISTANT_ID),
        "thread_id": bool(CAIA_THREAD_ID),
        "openai_base": OPENAI_BASE,
        "sentinel_actions_base": SENTINEL_ACTIONS_BASE,
    }
