# main.py  — Sentinel → Caia (Assistants v2) 최소안정판 (patched 2025-08-21)
# This patched version sanitizes the idempotency key to remove non‑ASCII characters.

# --- HUB FORWARDER (ASCII only) ---
import os, hmac, hashlib, json, asyncio, re
try:
    import httpx  # ensure in requirements
except Exception:
    httpx = None

HUB_URL = os.getenv("HUB_URL", "").strip()
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "").strip()

async def _forward_to_hub(raw: bytes, idem_key: str | None = None) -> None:
    if not HUB_URL or not CONNECTOR_SECRET:
        log.warning("Hub forward skipped: missing HUB_URL or CONNECTOR_SECRET")
        return
    
    if httpx is None:
        log.warning("Hub forward skipped: httpx not available")
        return
    
    try:
        from datetime import datetime, timezone
        
        # Parse original sentinel data
        original_data = json.loads(raw.decode("utf-8"))
        
        # Generate idempotency_key if not provided
        if not idem_key:
            ts = original_data.get("triggered_at", datetime.now(timezone.utc).isoformat())
            idx = original_data.get("index", "unknown")
            # sanitize idx to remove non‑ASCII characters and keep alphanumerics, hyphens and underscores
            idx_safe = ''.join(ch for ch in str(idx) if ord(ch) < 128 and (ch.isalnum() or ch in '-_'))
            # Create unique key from sanitized index and timestamp
            ts_clean = ts.replace(":", "").replace("-", "").replace(".", "").replace("+", "")[:14]
            idem_key = f"SN-{idx_safe}-{ts_clean}"
        else:
            # sanitize provided idempotency key as a precaution
            idem_key = ''.join(ch for ch in str(idem_key) if ord(ch) < 128 and (ch.isalnum() or ch in '-_'))
        
        # Create Hub-compatible payload structure
        hub_payload = {
            "idempotency_key": idem_key,  # REQUIRED!
            "source": "sentinel",
            "type": "alert.market",
            "priority": "medium",
            "timestamp": original_data.get("triggered_at", datetime.now(timezone.utc).isoformat()),
            "payload": {
                "index": original_data.get("index"),
                "level": original_data.get("level"),
                "delta_pct": original_data.get("delta_pct"),
                "note": original_data.get("note"),
                "original_ts": original_data.get("triggered_at")
            }
        }
        
        # Serialize the Hub-formatted data
        hub_body = json.dumps(hub_payload, ensure_ascii=False).encode("utf-8")
        
        # Calculate HMAC signature for the Hub-formatted body
        sig = hmac.new(CONNECTOR_SECRET.encode(), hub_body, hashlib.sha256).hexdigest()
        
        # Headers with both signature and idempotency key
        headers = {
            "Content-Type": "application/json",
            "X-Signature": sig,
            "Idempotency-Key": idem_key  # Hub checks both header and body
        }
        
        log.info("Hub forward attempt: %s to %s", idem_key, HUB_URL)  # 시도 로그 추가
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(HUB_URL, content=hub_body, headers=headers)
            if response.status_code == 200:
                log.info("Hub forward success: %s", idem_key)  # 성공 로그
            else:
                log.warning("Hub forward failed: %d - %s", response.status_code, response.text)  # 실패 로그
                
    except Exception as e:
        log.error("Hub forward error: %s", str(e))  # 에러 로그
# --- END HUB FORWARDER ---

import time, logging, requests, threading
from typing import Optional, Dict, Any, List
from collections import deque
from fastapi import FastAPI, Header, Request, Query

APP_VERSION = "sentinel-fastapi-v2-1.4.1-patched"

# ── ENV ──────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID      = os.getenv("CAIA_ASSISTANT_ID", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "")  # x-sentinel-key 로 사용
SENTINEL_ACTIONS_BASE = os.getenv("SENTINEL_ACTIONS_BASE", "https://fastapi-sentinel-production.up.railway.app").strip()
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()  # INFO level for production
CAIA_VERBOSE      = os.getenv("CAIA_VERBOSE", "0") == "1"   # 0:요약로그, 1:상세로그

# 타임아웃/폴링 ENV (필요 시 조정)
CONNECT_TIMEOUT   = float(os.getenv("CONNECT_TIMEOUT", "10"))
READ_TIMEOUT      = float(os.getenv("READ_TIMEOUT", "60"))   # ← 핵심: read 60s 이상
RUN_POLL_MAX_WAIT = int(os.getenv("RUN_POLL_MAX_WAIT", "90")) # ← 핵심: 폴링 최대 대기

def _env_int(name: str, default: int) -> int:
    import re as _re
    s = os.getenv(name, str(default)) or ""
    m = _re.search(r'\d+', s)
    return int(m.group()) if m else default

DEDUP_WINDOW_MIN  = _env_int("DEDUP_WINDOW_MIN", 30)
ALERT_CAP         = _env_int("ALERT_CAP", 2000)

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger("sentinel-fastapi-v2")

app = FastAPI(title="Sentinel FastAPI v2", version=APP_VERSION)

# ── DB증권 Router Integration (로그 설정 이후) ────────────────────────
# K200 선물 감시 - DB증권 API를 통한 주간/야간 선물 모니터링
DBSEC_ENABLE = os.getenv("DBSEC_ENABLE", "true").lower() in ["true", "1", "yes"]
if DBSEC_ENABLE:
    try:
        from routers.dbsec import router as dbsec_router
        app.include_router(dbsec_router)
        log.info("✅ DB증권 K200 선물지수 모니터링 활성화 (주간/야간)")
        log.info("   - 주간: 09:00-15:30 / 야간: 18:00-05:00")
        log.info("   - REST API 폴링 모드 사용")
    except Exception as e:
        log.warning("⚠️ DB증권 라우터 포함 실패: %s", e)
        log.info("🔄 기존 센티넬 시스템은 정상 작동합니다")
else:
    log.info("🚫 DB증권 K200 선물 모니터링 비활성화")

# Disable verbose logging from httpx, httpcore, and websocket libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("websocket").setLevel(logging.WARNING)
logging.getLogger("httpcore.connection").setLevel(logging.WARNING)
logging.getLogger("httpcore.http11").setLevel(logging.WARNING)

log.info("ENV: OPENAI=%s ASSIST=%s TG=%s KEY=%s INBOX=%s", 
         "SET" if OPENAI_API_KEY else "NO",
         ASSISTANT_ID[:10]+"..." if ASSISTANT_ID else "NO",
         "SET" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "NO",
         "SET" if SENTINEL_KEY else "NO",
         SENTINEL_ACTIONS_BASE)

# ── State ────────────────────────────────────────────────────────────
_last_fired: Dict[tuple, float] = {}      # (index, level) -> epoch
_alert_buf  = deque(maxlen=ALERT_CAP)     # 최신 알림이 좌측(0)

def within_dedup(idx: str, lvl: str) -> bool:
    now = time.time()
    k = (idx, lvl)
    last = _last_fired.get(k)
    if last and (now - last) < DEDUP_WINDOW_MIN * 60:
        return True
    _last_fired[k] = now
    return False

# ── DB증권 Router Integration은 위에서 처리됨 ────────────────────────

# ── Utils ────────────────────────────────────────────────────────────

def _oai_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "assistants=v2"}


def _format_msg(data: dict) -> str:
    delta = float(data["delta_pct"])
    msg = f"📡 [{str(data['level']).upper()}] {data['index']} {delta:+.2f}% / ⏱ {data['triggered_at']}"
    if data.get("note"): msg += f" / 📝 {data['note']}"
    return msg


def _append_inbox(item: dict) -> None:
    norm = {
        "index": str(item["index"]),
        "level": str(item["level"]).upper(),
        "delta_pct": float(item["delta_pct"]),
        "covix": item.get("covix"),
        "triggered_at": str(item["triggered_at"]),
        "note": (item.get("note") or None),
    }
    _alert_buf.appendleft(norm)


def send_telegram(text: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=(CONNECT_TIMEOUT, 15),  # 텔레그램은 짧게 유지
        )
        return r.ok
    except Exception:
        return False

# ── Actions 호출(툴 백엔드) ─────────────────────────────────────────
# ※ inbox는 상대적으로 짧아도 되지만, 불필요 타임아웃 방지를 위해 read 30s 권장
ACTIONS_TIMEOUT_SEC = float(os.getenv("ACTIONS_TIMEOUT_SEC", "30"))
ACTIONS_RETRIES     = int(os.getenv("ACTIONS_RETRIES", "2"))
ACTIONS_BACKOFF_SEC = float(os.getenv("ACTIONS_BACKOFF_SEC", "0.8"))


def _fetch_latest_alerts(limit=10, level_min: Optional[str]=None,
                         index: Optional[str]=None, since: Optional[str]=None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": int(limit)}
    if level_min: params["level_min"] = level_min
    if index:     params["index"]     = index
    if since:     params["since"]     = since  # 상위 호출부에서 +09:00 ISO 보장 권장

    url = f"{SENTINEL_ACTIONS_BASE}/sentinel/inbox"

    headers: Dict[str, str] = {}
    if SENTINEL_KEY:
        headers["x-sentinel-key"] = SENTINEL_KEY

    for attempt in range(ACTIONS_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=(CONNECT_TIMEOUT, ACTIONS_TIMEOUT_SEC))
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("items") is not None:
                return data
        except Exception as e:
            log.warning("[Actions] inbox call failed (%d/%d): %s", attempt+1, ACTIONS_RETRIES, e)
        if attempt < ACTIONS_RETRIES:
            time.sleep(ACTIONS_BACKOFF_SEC)

    # 최후 보호
    return {"items":[{"index":"SYSTEM","level":"LV1","delta_pct":0,
                       "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                       "note":"actions timeout/retry exhausted"}]}

# ── Assistants v2 Run 폴링 ───────────────────────────────────────────

def _get_run(base: str, headers: Dict[str,str], thread_id: str, run_id: str):
    r = requests.get(f"{base}/threads/{thread_id}/runs/{run_id}", headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    return (r.ok, r.json() if r.ok else r.text, r.status_code)


def _get_steps(base: str, headers: Dict[str,str], thread_id: str, run_id: str):
    r = requests.get(f"{base}/threads/{thread_id}/runs/{run_id}/steps", headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    return (r.ok, r.json() if r.ok else r.text, r.status_code)


def _poll_and_submit_tools(thread_id: str, run_id: str, max_wait_sec: int = RUN_POLL_MAX_WAIT) -> Dict[str, Any]:
    base = "https://api.openai.com/v1"
    headers = _oai_headers()
    start = time.time()

    while True:
        ok, cur, code = _get_run(base, headers, thread_id, run_id)
        if not ok:
            return {"ok": False, "stage": "get_run", "status": code, "resp": cur, "run_id": run_id}

        st = cur.get("status")
        if st in ("queued","in_progress","cancelling"):
            if time.time() - start > max_wait_sec:
                return {"ok": False, "stage": "timeout", "status": st, "run_id": run_id}
            time.sleep(0.8); continue

        if st == "requires_action":
            ra = cur.get("required_action",{}).get("submit_tool_outputs",{})
            calls: List[Dict[str, Any]] = ra.get("tool_calls",[]) or []
            outs: List[Dict[str, str]]  = []

            for c in calls:
                fn   = c.get("function",{}).get("name")
                args = c.get("function",{}).get("arguments") or "{}"
                try: parsed = json.loads(args)
                except Exception: parsed = {}

                if fn == "getLatestAlerts":
                    try: limit_val = int(parsed.get("limit", 10))
                    except Exception: limit_val = 10
                    data = _fetch_latest_alerts(
                        limit     = limit_val,
                        level_min = parsed.get("level_min"),
                        index     = parsed.get("index"),
                        since     = parsed.get("since"),
                    )
                    outs.append({"tool_call_id": c["id"], "output": json.dumps(data, ensure_ascii=False)})
                else:
                    outs.append({"tool_call_id": c["id"], "output": json.dumps({"error": f"unknown function {fn}"}, ensure_ascii=False)})

            r2 = requests.post(
                f"{base}/threads/{thread_id}/runs/{run_id}/submit_tool_outputs",
                headers=headers,
                json={"tool_outputs": outs},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            if not r2.ok:
                return {"ok": False, "stage": "submit_tool_outputs", "status": r2.status_code, "resp": r2.text, "run_id": run_id}
            time.sleep(0.6); continue

        # 완료/실패/취소 → 요약 로그
        ok_steps, steps, sc = _get_steps(base, headers, thread_id, run_id)
        if CAIA_VERBOSE:
            log.info("[CAIA] steps: %s", json.dumps(steps if ok_steps else {"code":sc,"text":steps}, ensure_ascii=False))
        return {"ok": (st=="completed"), "status": st, "run_id": run_id}

# ── Assistants v2 호출 (비동기용) ────────────────────────────────────

def send_caia_v2(text: str) -> Dict[str, Any]:
    if not (OPENAI_API_KEY and ASSISTANT_ID):
        return {"ok": False, "stage": "precheck", "reason": "OPENAI/ASSISTANT env not set"}
    thread_id = os.getenv("CAIA_THREAD_ID","").strip()
    if not thread_id:
        return {"ok": False, "stage": "precheck", "reason": "CAIA_THREAD_ID not set"}

    base = "https://api.openai.com/v1"
    headers = _oai_headers()

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
                    "since":     {"type": "string"}
                }
            }
        }
    }]

    run_body = {
        "assistant_id": ASSISTANT_ID,
        "tools": tools_def,
        "tool_choice": {"type": "function", "function": {"name": "getLatestAlerts"}},
        "truncation_strategy": {"type": "last_messages", "last_messages": 8},
        "parallel_tool_calls": False,
        "instructions": (
            "센티넬/알람 키워드 감지. getLatestAlerts(limit=10 기본) 호출해 최근 알림을 요약하라. "
            "응답: (지표, 레벨, Δ%, 시각, note) 리스트. 마지막에 '전략 판단 들어갈까요?'만 질문."
        ),
        "additional_messages": [
            {"role":"user","content":[{"type":"text","text": text}]}
        ],
    }

    # Run 생성 → requires_action 처리
    r = requests.post(f"{base}/threads/{thread_id}/runs", headers=headers, json=run_body, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    if not r.ok:
        # 활성 런 충돌 등은 짧게 대기 후 1회 재시도
        if "활성화되어 있는 동안" in (r.text or "") or "active" in (r.text or "").lower():
            time.sleep(1.2)
            r = requests.post(f"{base}/threads/{thread_id}/runs", headers=headers, json=run_body, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if not r.ok:
            return {"ok": False, "stage": "run", "status": r.status_code, "resp": r.text, "thread_id": thread_id}

    run_id = r.json().get("id","")
    return _poll_and_submit_tools(thread_id, run_id, max_wait_sec=RUN_POLL_MAX_WAIT)


def _run_caia_job(text: str) -> None:
    try:
        info = send_caia_v2(text)
        if CAIA_VERBOSE:
            log.info("[CAIA-JOB] done: %s", json.dumps(info, ensure_ascii=False))
        else:
            log.info("[CAIA-JOB] status=%s run_id=%s", info.get("status"), info.get("run_id"))
    except Exception as e:
        log.exception("[CAIA-JOB] exception: %s", e)


def trigger_caia_async(text: str) -> None:
    threading.Thread(target=_run_caia_job, args=(text,), daemon=True).start()

# ── HTTP ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    """Root endpoint - system info"""
    return {
        "service": "Sentinel FastAPI v2",
        "status": "operational",
        "version": APP_VERSION,
        "endpoints": {
            "health": "/health",
            "alert": "/sentinel/alert",
            "inbox": "/sentinel/inbox",
            "dbsec": "/sentinel/dbsec/health"
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    }

@app.get("/health")
def health():
    return {
        "status":"ok","version":APP_VERSION,
        "assistant_set": bool(ASSISTANT_ID),
        "tg_set": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "thread_fixed": bool(os.getenv("CAIA_THREAD_ID","").strip()),
        "dedup_min": DEDUP_WINDOW_MIN,
        "alert_buf_len": len(_alert_buf),
        "alert_cap": ALERT_CAP,
    }


@app.post("/sentinel/alert")
async def sentinel_alert(request: Request, x_sentinel_key: Optional[str] = Header(default=None)):
    """
    - 입력을 즉시 수신/검증 → 텔레그램 전송 → 카이아 비동기 트리거 → 200 응답
    - 카이아 툴콜 실패해도 HTTP는 200로, 상세는 로그/텔레메트리로 본다
    """
    try:
        raw = await request.body()
        try: data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError: data = json.loads(raw.decode("utf-8", errors="replace"))

        if SENTINEL_KEY and x_sentinel_key != SENTINEL_KEY:
            return {"status": "error", "where": "auth", "detail": "invalid sentinel key"}

        for f in ("index","level","delta_pct","triggered_at"):
            if f not in data:
                return {"status":"error","where":"payload","detail":f"missing field {f}"}

        lvl = str(data["level"]).upper()
        if lvl not in {"LV1","LV2","LV3","CLEARED","BREACH","RECOVER"}:
            lvl = "LV2"

        idx = str(data["index"])
        if lvl != "CLEARED" and within_dedup(idx, lvl):
            _append_inbox(data)
            return {"status":"dedup_suppressed","reason":f"same alert within {DEDUP_WINDOW_MIN} minutes"}

        data["level"] = lvl
        msg = _format_msg(data)

        tg_ok = send_telegram(msg)
        trigger_caia_async(msg)
        _append_inbox(data)

        # Hub forwarding (fire-and-forget)
        body_bytes = raw  # Already have the raw bytes
        
        # Extract idempotency_key from JSON
        idem_key = None
        try:
            idem_key = data.get("idempotency_key")
        except Exception:
            pass
        
        # Fire-and-forget forwarding
        asyncio.create_task(_forward_to_hub(body_bytes, idem_key))
        
        return {"status":"delivered","telegram":tg_ok,"caia":{"queued":True},"message":msg}
    
    except Exception as e:
        log.exception("sentinel_alert error: %s", e)
        return {"status":"error","where":"server","detail":str(e)}


@app.get("/sentinel/inbox")
def sentinel_inbox(
    limit: int = Query(10, ge=1, le=50),
    level_min: Optional[str] = Query(None, pattern=r"^LV[1-3]$"),
    index: Optional[str] = None,
    since: Optional[str] = None,
):
    def _rank(lv: str) -> int: return {"LV1":1,"LV2":2,"LV3":3}.get(lv,0)
    items = list(_alert_buf)

    if level_min:
        minv = _rank(level_min.upper())
        items = [x for x in items if _rank(x["level"]) >= minv]
    if index:
        items = [x for x in items if x["index"] == index]
    if since:
        items = [x for x in items if x["triggered_at"] >= since]

    return {"items": items[:limit]}

# Procfile:
# web: uvicorn main:app --host 0.0.0.0 --port $PORT