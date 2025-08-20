# main.py  (Assistants API v2 · Caia 연동 안정판 / async 고정)
import os
import time
import json
import logging
import requests
import threading
from typing import Optional, Dict, Any, List
from collections import deque
from fastapi import FastAPI, Header, Request, Query

APP_VERSION = "sentinel-fastapi-v2-1.3.4"

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="Sentinel FastAPI v2", version=APP_VERSION)

# ── ENV ───────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID      = os.getenv("CAIA_ASSISTANT_ID", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "")
SENTINEL_ACTIONS_BASE = (os.getenv("SENTINEL_ACTIONS_BASE", "https://fastapi-sentinel-production.up.railway.app") or "").strip()
LOG_LEVEL         = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()

def _env_int(key: str, default: int) -> int:
    import re
    v = os.getenv(key, str(default)) or ""
    m = re.search(r"\d+", v)
    return int(m.group()) if m else default

DEDUP_WINDOW_MIN  = _env_int("DEDUP_WINDOW_MIN", 30)
ALERT_CAP         = _env_int("ALERT_CAP", 2000)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("sentinel-fastapi-v2")

log.info("=" * 60)
log.info("ENV: OAI_KEY=%s, ASSISTANT=%s, TG=%s, S_KEY=%s, ACTIONS_BASE=%s",
         "SET" if OPENAI_API_KEY else "NOT", ASSISTANT_ID[:16] + "..." if ASSISTANT_ID else "NOT",
         "SET" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "NOT",
         "SET" if SENTINEL_KEY else "NOT", SENTINEL_ACTIONS_BASE)
log.info("DEDUP=%d min, ALERT_CAP=%d", DEDUP_WINDOW_MIN, ALERT_CAP)
log.info("=" * 60)

# ── Ring buffer & dedup ───────────────────────────────────────────────
_last_fired: Dict[tuple, float] = {}             # key=(index,level) -> epoch
_alert_buf: deque = deque(maxlen=ALERT_CAP)      # newest first (appendleft)

def within_dedup(idx: str, lvl: str) -> bool:
    now = time.time()
    k = (idx, lvl)
    last = _last_fired.get(k)
    if last and (now - last) < DEDUP_WINDOW_MIN * 60:
        return True
    _last_fired[k] = now
    return False

# ── Telegram ──────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.warning("Telegram env not set → skip")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if not r.ok:
            log.error("Telegram fail %s %s", r.status_code, r.text)
        return r.ok
    except Exception as e:
        log.exception("Telegram exception: %s", e)
        return False

# ── Assistants v2 helpers ────────────────────────────────────────────
def _oai_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }

# 안정화 상수(ENV 추가 없이 고정)
ACTIONS_TIMEOUT_SEC = 15.0
ACTIONS_RETRIES     = 2
ACTIONS_BACKOFF_SEC = 0.8

def _fetch_latest_alerts(limit=10, level_min: Optional[str]=None,
                         index: Optional[str]=None, since: Optional[str]=None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": int(limit)}
    if level_min: params["level_min"] = level_min
    if index:     params["index"]     = index
    if since:     params["since"]     = since

    url = f"{SENTINEL_ACTIONS_BASE}/sentinel/inbox"

    for attempt in range(ACTIONS_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=ACTIONS_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("items"):
                return data
            log.warning("[CAIA] inbox empty (try %d/%d)", attempt+1, ACTIONS_RETRIES)
        except Exception as e:
            log.error("[CAIA] inbox call failed (try %d/%d): %s", attempt+1, ACTIONS_RETRIES, e)
        if attempt < ACTIONS_RETRIES:
            time.sleep(ACTIONS_BACKOFF_SEC * (attempt+1))

    # fallback 한 건이라도 반환
    return {"items": [{
        "index": "SYSTEM", "level": "LV1", "delta_pct": 0,
        "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "note": f"Actions 호출 실패: timeout/retry exhausted (url={url})"
    }]}

def _get_run(base: str, headers: Dict[str,str], thread_id: str, run_id: str) -> Dict[str, Any]:
    r = requests.get(f"{base}/threads/{thread_id}/runs/{run_id}", headers=headers, timeout=12)
    ok = r.ok
    return {"ok": ok, "status_code": r.status_code, "json": (r.json() if ok else None), "text": (r.text if not ok else None)}

def _get_run_steps(base: str, headers: Dict[str,str], thread_id: str, run_id: str) -> Dict[str, Any]:
    r = requests.get(f"{base}/threads/{thread_id}/runs/{run_id}/steps", headers=headers, timeout=12)
    ok = r.ok
    return {"ok": ok, "status_code": r.status_code, "json": (r.json() if ok else None), "text": (r.text if not ok else None)}

def _poll_and_submit_tools(thread_id: str, run_id: str, max_wait_sec: int = 20) -> Dict[str, Any]:
    """
    requires_action 처리: getLatestAlerts 호출 → submit_tool_outputs → 완료까지 단기 폴링
    (백그라운드 스레드에서 실행됨: HTTP 502와 분리)
    """
    base = "https://api.openai.com/v1"
    headers = _oai_headers()
    start = time.time()

    while True:
        info = _get_run(base, headers, thread_id, run_id)
        if not info["ok"]:
            return {"ok": False, "stage": "get_run", "status": info["status_code"], "resp": info["text"], "run_id": run_id}

        cur = info["json"]
        st  = cur.get("status")

        if st in ("queued", "in_progress", "cancelling"):
            if time.time() - start > max_wait_sec:
                diag = {"status": st, "last_error": cur.get("last_error"), "incomplete_details": cur.get("incomplete_details")}
                return {"ok": False, "stage": "timeout", "run_id": run_id, "diag": diag}
            time.sleep(0.7)
            continue

        if st == "requires_action":
            ra = cur.get("required_action", {}).get("submit_tool_outputs", {})
            calls: List[Dict[str, Any]] = ra.get("tool_calls", []) or []
            outs: List[Dict[str, str]]  = []

            for c in calls:
                fn   = c.get("function", {}).get("name")
                args = c.get("function", {}).get("arguments") or "{}"
                try:
                    parsed = json.loads(args)
                except Exception:
                    parsed = {}

                if fn == "getLatestAlerts":
                    limit_val = parsed.get("limit", 10)
                    try:
                        limit_val = int(limit_val)
                    except Exception:
                        limit_val = 10
                    data = _fetch_latest_alerts(
                        limit     = limit_val,
                        level_min = parsed.get("level_min"),
                        index     = parsed.get("index"),
                        since     = parsed.get("since"),
                    )
                    outs.append({"tool_call_id": c["id"], "output": json.dumps(data, ensure_ascii=False)})
                else:
                    outs.append({"tool_call_id": c["id"],
                                 "output": json.dumps({"error": f"unknown function {fn}"}, ensure_ascii=False)})

            r2 = requests.post(
                f"{base}/threads/{thread_id}/runs/{run_id}/submit_tool_outputs",
                headers=headers,
                json={"tool_outputs": outs},
                timeout=15,
            )
            if not r2.ok:
                return {"ok": False, "stage": "submit_tool_outputs", "status": r2.status_code, "resp": r2.text, "run_id": run_id}
            time.sleep(0.5)
            continue

        # completed / failed / cancelled → 진단 포함
        steps = _get_run_steps(base, headers, thread_id, run_id)
        diag = {
            "status": st,
            "last_error": cur.get("last_error"),
            "incomplete_details": cur.get("incomplete_details"),
            "step_status": steps.get("status_code"),
            "steps": (steps.get("json") or steps.get("text"))
        }
        return {"ok": (st == "completed"), "status": st, "run_id": run_id, "diag": diag}

def send_caia_v2(text: str) -> Dict[str, Any]:
    """
    Assistants API v2
    - Run 생성 시 additional_messages 로 유저 메시지 동시 전달(활성 Run/메시지 충돌 회피)
    - tools(definition) + tool_choice(getLatestAlerts) 동시 지정
    - requires_action 발생 시 /sentinel/inbox 직접 호출 → submit_tool_outputs
    """
    if not (OPENAI_API_KEY and ASSISTANT_ID):
        return {"ok": False, "stage": "precheck", "reason": "OPENAI/ASSISTANT env not set"}

    base = "https://api.openai.com/v1"
    headers = _oai_headers()

    try:
        thread_id = (os.getenv("CAIA_THREAD_ID", "") or "").strip()
        if not thread_id:
            return {"ok": False, "stage": "precheck", "reason": "CAIA_THREAD_ID not set"}

        tools_def = [{
            "type": "function",
            "function": {
                "name": "getLatestAlerts",
                "description": "최근 센티넬 알림 조회",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit":     {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                        "level_min": {"type": "string", "enum": ["LV1", "LV2", "LV3"]},
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
                "센티넬/알람 키워드 감지. getLatestAlerts(limit=10 기본)을 호출해 최근 알림을 요약하라. "
                "응답 형식: (지표, 레벨, Δ%, 시각, note) 한 줄 리스트. "
                "전략 판단은 지금 하지 말고, 마지막에 '전략 판단 들어갈까요?'만 질문."
            ),
            "additional_messages": [
                {"role": "user", "content": [{"type": "text", "text": text}]}
            ],
        }

        r2 = requests.post(f"{base}/threads/{thread_id}/runs", headers=headers, json=run_body, timeout=15)
        if not r2.ok:
            return {"ok": False, "stage": "run", "status": r2.status_code, "resp": r2.text, "thread_id": thread_id}

        run_id = r2.json().get("id", "")
        done = _poll_and_submit_tools(thread_id, run_id, max_wait_sec=20)
        done["thread_id"] = thread_id
        return done

    except Exception as e:
        log.exception("OpenAI exception: %s", e)
        return {"ok": False, "stage": "exception", "reason": str(e)}

# ── Utils ─────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "assistant_set": bool(ASSISTANT_ID),
        "tg_set": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "thread_fixed": bool((os.getenv("CAIA_THREAD_ID", "") or "").strip()),
        "dedup_min": DEDUP_WINDOW_MIN,
        "alert_buf_len": len(_alert_buf),
        "alert_cap": ALERT_CAP,
    }

def _format_msg(data: dict) -> str:
    delta = float(data["delta_pct"])
    covix = data.get("covix")
    msg = f"📡 [{str(data['level']).upper()}] {data['index']} {delta:+.2f}%"
    if covix is not None:
        try:
            msg += f" / COVIX {float(str(covix)):+.2f}"
        except Exception:
            msg += f" / COVIX {covix}"
    msg += f" / ⏱ {data['triggered_at']}"
    if data.get("note"):
        msg += f" / 📝 {data['note']}"
    return msg

def _append_inbox(data: dict) -> None:
    item = {
        "index":        str(data["index"]),
        "level":        str(data["level"]).upper(),
        "delta_pct":    float(data["delta_pct"]),
        "covix":        (None if data.get("covix") in (None, "")
                         else float(str(data.get("covix")).replace("+",""))
                         if str(data.get("covix")).replace(".","",1).lstrip("+-").isdigit()
                         else data.get("covix")),
        "triggered_at": str(data["triggered_at"]),
        "note":         (data.get("note") or None),
    }
    _alert_buf.appendleft(item)

# ── Caia 호출: 항상 비동기(HTTP 즉시 응답) ───────────────────────────
def _run_caia_job(text: str) -> None:
    try:
        info = send_caia_v2(text)
        log.info("[CAIA-JOB] done: %s", json.dumps(info, ensure_ascii=False))
    except Exception as e:
        log.exception("[CAIA-JOB] exception: %s", e)

def trigger_caia_async(text: str) -> None:
    th = threading.Thread(target=_run_caia_job, args=(text,), daemon=True)
    th.start()

# ── Endpoints ─────────────────────────────────────────────────────────
@app.post("/sentinel/alert")
async def sentinel_alert(
    request: Request,
    x_sentinel_key: Optional[str] = Header(default=None)
):
    """
    - 즉시 200 응답(accepted) 후, 카이아 연동은 내부 스레드에서 비동기 처리 → 502 원천 차단
    - Caia는 Run 생성 시 tools + tool_choice로 getLatestAlerts를 강제 호출, requires_action은 서버가 처리
    """
    try:
        raw = await request.body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            data = json.loads(raw.decode("utf-8", errors="replace"))

        # 공유키 검증(옵션)
        if SENTINEL_KEY and x_sentinel_key != SENTINEL_KEY:
            return {"status": "error", "where": "auth", "detail": "invalid sentinel key"}

        # 필수 필드 검증
        for f in ("index", "level", "delta_pct", "triggered_at"):
            if f not in data:
                return {"status": "error", "where": "payload", "detail": f"missing field {f}"}

        lvl = str(data["level"]).upper()
        valid = {"LV1", "LV2", "LV3", "CLEARED", "BREACH", "RECOVER"}
        if lvl not in valid:
            log.warning("Unknown level: %s → LV2", lvl)
            lvl = "LV2"

        idx = str(data["index"])

        # dedup (CLEARED는 통과)
        if lvl != "CLEARED" and within_dedup(idx, lvl):
            _append_inbox(data)
            return {"status": "dedup_suppressed", "reason": f"same alert within {DEDUP_WINDOW_MIN} minutes"}

        data["level"] = lvl
        msg = _format_msg(data)

        log.info("알림 전송: %s %s %.2f%% - %s", idx, lvl, float(data.get("delta_pct", 0)), data.get("note", ""))

        # Telegram (non-blocking)
        tg_ok = send_telegram(msg)

        # Caia 연동은 비동기
        trigger_caia_async(msg)

        # inbox 저장
        _append_inbox({
            "index": idx, "level": lvl, "delta_pct": float(data["delta_pct"]),
            "covix": data.get("covix"), "triggered_at": data["triggered_at"],
            "note": data.get("note")
        })

        return {"status": "delivered", "telegram": tg_ok, "caia": {"ok": True, "queued": True, "mode": "async"}, "message": msg}

    except Exception as e:
        log.exception("sentinel_alert exception: %s", e)
        return {"status": "error", "where": "server", "detail": str(e)}

@app.get("/sentinel/inbox")
def sentinel_inbox(
    limit: int = Query(10, ge=1, le=50),
    level_min: Optional[str] = Query(None, pattern=r"^LV[1-3]$"),
    index: Optional[str] = None,
    since: Optional[str] = None,
):
    """
    커스텀 GPT Action(getLatestAlerts)이 호출하는 읽기 전용 API (단기 메모리)
    """
    def lv_rank(lv: str) -> int:
        return {"LV1": 1, "LV2": 2, "LV3": 3}.get(lv, 0)

    items = list(_alert_buf)  # newest first

    if level_min:
        minv = lv_rank(level_min.upper())
        items = [x for x in items if lv_rank(x["level"]) >= minv]

    if index:
        items = [x for x in items if x["index"] == index]

    if since:
        items = [x for x in items if x["triggered_at"] >= since]

    return {"items": items[:limit]}

# Procfile (참고):
# web: uvicorn main:app --host 0.0.0.0 --port $PORT
