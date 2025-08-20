# main.py  (Assistants API v2, thread 고정 + inbox 지원, tools+tool_choice+requires_action 처리)
import os
import time
import json
import logging
import requests
from typing import Optional, Dict, Any, List
from collections import deque
from fastapi import FastAPI, Header, Request, Query

APP_VERSION = "sentinel-fastapi-v2-1.3.3"

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="Sentinel FastAPI v2", version=APP_VERSION)

# ── 환경변수 ──────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID      = os.getenv("CAIA_ASSISTANT_ID", "")     # v2 Assistant ID
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "")
SENTINEL_ACTIONS_BASE = os.getenv(
    "SENTINEL_ACTIONS_BASE",
    "https://fastapi-sentinel-production.up.railway.app"
).strip()
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()

def parse_int_env(key: str, default: int) -> int:
    import re
    value = os.getenv(key, str(default)) or ""
    m = re.search(r'\d+', value)
    return int(m.group()) if m else default

DEDUP_WINDOW_MIN  = parse_int_env("DEDUP_WINDOW_MIN", 30)
ALERT_CAP         = parse_int_env("ALERT_CAP", 2000)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("sentinel-fastapi-v2")

log.info("=" * 60)
log.info("Sentinel FastAPI v2 ENV:")
log.info("  OPENAI_API_KEY: %s", "SET" if OPENAI_API_KEY else "NOT SET")
log.info("  ASSISTANT_ID: %s", ASSISTANT_ID[:20] + "..." if ASSISTANT_ID else "NOT SET")
log.info("  TELEGRAM: %s", "SET" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "NOT SET")
log.info("  SENTINEL_KEY: %s", "SET" if SENTINEL_KEY else "NOT SET")
log.info("  SENTINEL_ACTIONS_BASE: %s", SENTINEL_ACTIONS_BASE)
log.info("  DEDUP_WINDOW_MIN: %d", DEDUP_WINDOW_MIN)
log.info("  ALERT_CAP: %d", ALERT_CAP)
log.info("=" * 60)

# ── 중복 억제 및 링버퍼 ──────────────────────────────────────────────
_last_fired: Dict[tuple, float] = {}    # key=(index,level) -> epoch
_alert_buf  = deque(maxlen=ALERT_CAP)   # 최신 알림이 좌측(0)에 오도록 appendleft

def within_dedup(idx: str, lvl: str) -> bool:
    now = time.time()
    k = (idx, lvl)
    last = _last_fired.get(k)
    if last and (now - last) < DEDUP_WINDOW_MIN * 60:
        return True
    _last_fired[k] = now
    return False

# ── 외부 전송: 텔레그램 ──────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.warning("Telegram env 미설정 → 스킵")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if not r.ok:
            log.error("Telegram 실패 %s %s", r.status_code, r.text)
        return r.ok
    except Exception as e:
        log.exception("Telegram 예외: %s", e)
        return False

# ── Assistants v2 헬퍼 ───────────────────────────────────────────────
def _oai_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }

def _parse_wait_seconds_from_message(msg: str, default_sec: float = 9.0) -> float:
    """
    오류 메시지 안의 'Please try again in X.XXXs'에서 X를 뽑아냄.
    실패하면 default_sec 반환.
    """
    try:
        import re
        m = re.search(r"try again in ([0-9]+(?:\.[0-9]+)?)s", msg)
        if m:
            return float(m.group(1)) + 0.5  # 약간의 여유
    except Exception:
        pass
    return default_sec

def _fetch_latest_alerts(limit=10, level_min: Optional[str]=None,
                         index: Optional[str]=None, since: Optional[str]=None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": int(limit)}
    if level_min: params["level_min"] = level_min
    if index:     params["index"]     = index
    if since:     params["since"]     = since
    try:
        r = requests.get(f"{SENTINEL_ACTIONS_BASE}/sentinel/inbox", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not data.get("items"):
            data = {"items":[{"index":"SYSTEM","level":"LV1","delta_pct":0,
                              "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                              "note":"최근 알림 없음 (fallback)"}]}
        return data
    except Exception as e:
        log.error("[CAIA] _fetch_latest_alerts failed: %s", e)
        return {"items":[{"index":"SYSTEM","level":"LV1","delta_pct":0,
                          "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                          "note": f"Actions 호출 실패: {e}"}]}

def _get_run(base: str, headers: Dict[str,str], thread_id: str, run_id: str) -> Dict[str, Any]:
    r = requests.get(f"{base}/threads/{thread_id}/runs/{run_id}", headers=headers, timeout=12)
    ok = r.ok
    return {"ok": ok, "status_code": r.status_code, "json": (r.json() if ok else None), "text": (r.text if not ok else None)}

def _get_run_steps(base: str, headers: Dict[str,str], thread_id: str, run_id: str) -> Dict[str, Any]:
    r = requests.get(f"{base}/threads/{thread_id}/runs/{run_id}/steps", headers=headers, timeout=12)
    ok = r.ok
    return {"ok": ok, "status_code": r.status_code, "json": (r.json() if ok else None), "text": (r.text if not ok else None)}

def _poll_and_submit_tools(thread_id: str, run_id: str, max_wait_sec: int = 25) -> Dict[str, Any]:
    """
    requires_action 처리: getLatestAlerts 호출 → submit_tool_outputs → 완료까지 단기 폴링
    실패 시 last_error/steps 등 진단(diag) 포함해서 반환.
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

        # completed / failed / cancelled → 진단 포함하여 반환
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
    - Run 생성 시 tools(definition) + tool_choice(getLatestAlerts) 동시 지정
    - requires_action 발생 시 /sentinel/inbox 직접 호출 → submit_tool_outputs
    - 레이트리밋 자동 재시도 + 히스토리 절단(truncation)
    - 디버그 정보를 dict로 반환
    """
    if not (OPENAI_API_KEY and ASSISTANT_ID):
        return {"ok": False, "stage": "precheck", "reason": "OPENAI/ASSISTANT env not set"}

    base = "https://api.openai.com/v1"
    headers = _oai_headers()

    try:
        thread_id = os.getenv("CAIA_THREAD_ID", "").strip()
        if not thread_id:
            return {"ok": False, "stage": "precheck", "reason": "CAIA_THREAD_ID not set"}

        # v2 포맷: content 배열
        msg_body = {
            "role": "user",
            "content": [
                {"type": "text", "text": text}
            ]
        }

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
            "tool_choice": {
                "type": "function",
                "function": {"name": "getLatestAlerts"}
            },
            # 히스토리 절단: 최근 8개 메시지만 모델에 공급 (입력 토큰 절감)
            "truncation_strategy": {"type": "auto", "last_messages": 8},
            # 출력 토큰 절제
            "max_output_tokens": 512,
            # 지시문은 짧고 명확하게
            "instructions": (
                "센티넬/알람 키워드 감지. getLatestAlerts(limit=10) 호출로 최근 알림 요약. "
                "형식: (지표, 레벨, Δ%, 시각, note) 리스트. 마지막에 '전략 판단 들어갈까요?' 질문."
            ),
            # 복잡도/소비량 줄이기
            "parallel_tool_calls": False,
        }

        attempts = 3
        for attempt in range(1, attempts + 1):
            # 1) 메시지 추가
            r1 = requests.post(f"{base}/threads/{thread_id}/messages", headers=headers, json=msg_body, timeout=12)
            if r1.status_code == 404:
                return {"ok": False, "stage": "message", "status": 404, "resp": r1.text, "thread_id": thread_id}
            if not r1.ok:
                if r1.status_code == 429 and attempt < attempts:
                    wait = _parse_wait_seconds_from_message(r1.text)
                    time.sleep(wait)
                    continue
                return {"ok": False, "stage": "message", "status": r1.status_code, "resp": r1.text, "thread_id": thread_id}

            # 2) Run 생성
            r2 = requests.post(f"{base}/threads/{thread_id}/runs", headers=headers, json=run_body, timeout=15)
            if not r2.ok:
                if r2.status_code == 429 and attempt < attempts:
                    wait = _parse_wait_seconds_from_message(r2.text)
                    time.sleep(wait)
                    continue
                return {"ok": False, "stage": "run", "status": r2.status_code, "resp": r2.text, "thread_id": thread_id}

            run_id = r2.json().get("id", "")

            # 3) requires_action 처리 + 완료 대기
            done = _poll_and_submit_tools(thread_id, run_id, max_wait_sec=30)

            # 런 실패 사유가 rate_limit_exceeded면 재시도
            diag_str = json.dumps(done.get("diag", {}), ensure_ascii=False)
            if not done.get("ok") and "rate_limit_exceeded" in diag_str and attempt < attempts:
                wait = _parse_wait_seconds_from_message(diag_str)
                time.sleep(wait)
                continue

            done["thread_id"] = thread_id
            return done

        return {"ok": False, "stage": "retry_exhausted", "reason": "rate_limit_exceeded or 429"}

    except Exception as e:
        log.exception("OpenAI 예외: %s", e)
        return {"ok": False, "stage": "exception", "reason": str(e)}

# ── 유틸 ─────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "assistant_set": bool(ASSISTANT_ID),
        "tg_set": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "thread_fixed": bool(os.getenv("CAIA_THREAD_ID", "").strip()),
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

# ── 엔드포인트 ───────────────────────────────────────────────────────
@app.post("/sentinel/alert")
async def sentinel_alert(
    request: Request,
    x_sentinel_key: Optional[str] = Header(default=None)
):
    """
    - 실패해도 200 JSON으로 원인 반환 (server 500 방지)
    - 카이아 툴콜은 tools+tool_choice로 강제, requires_action은 서버가 처리
    """
    try:
        # 본문 안전 파싱(UTF-8 아닌 경우도 대체문자 허용)
        raw = await request.body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            data = json.loads(raw.decode("utf-8", errors="replace"))

        # (선택) 공유키 검증
        if SENTINEL_KEY and x_sentinel_key != SENTINEL_KEY:
            return {"status": "error", "where": "auth", "detail": "invalid sentinel key"}

        # 필수 필드 검증
        for f in ("index", "level", "delta_pct", "triggered_at"):
            if f not in data:
                return {"status": "error", "where": "payload", "detail": f"missing field {f}"}

        lvl = str(data["level"]).upper()
        valid_levels = {"LV1", "LV2", "LV3", "CLEARED", "BREACH", "RECOVER"}
        if lvl not in valid_levels:
            log.warning("예상치 않은 레벨: %s, LV2로 처리", lvl)
            lvl = "LV2"

        idx = str(data["index"])

        # 중복 억제 (CLEARED는 항상 통과)
        if lvl != "CLEARED" and within_dedup(idx, lvl):
            _append_inbox(data)
            return {"status": "dedup_suppressed", "reason": f"same alert within {DEDUP_WINDOW_MIN} minutes"}

        # 메시지 생성
        data["level"] = lvl
        msg = _format_msg(data)

        log.info("알림 전송: %s %s %.2f%% - %s",
                 idx, lvl, float(data.get("delta_pct", 0)),
                 data.get("note", ""))

        # 텔레그램
        tg_ok = send_telegram(msg)

        # 카이아(Assistants v2) — 툴콜 강제 + tools 정의 + requires_action 처리
        caia_info = send_caia_v2(msg)

        # inbox 적재
        _append_inbox({
            "index": idx, "level": lvl, "delta_pct": float(data["delta_pct"]),
            "covix": data.get("covix"), "triggered_at": data["triggered_at"],
            "note": data.get("note")
        })

        return {
            "status": "delivered",
            "telegram": tg_ok,
            "caia": caia_info,   # 성공/실패/사유/단계/steps 모두 확인 가능
            "message": msg
        }

    except Exception as e:
        log.exception("sentinel_alert 예외: %s", e)
        return {"status": "error", "where": "server", "detail": str(e)}

@app.get("/sentinel/inbox")
def sentinel_inbox(
    limit: int = Query(10, ge=1, le=50),
    level_min: Optional[str] = Query(None, pattern=r"^LV[1-3]$"),
    index: Optional[str] = None,
    since: Optional[str] = None,
):
    """커스텀 GPT Action이 호출하는 읽기 전용 API (단기 메모리)."""
    def lv_rank(lv: str) -> int:
        return {"LV1": 1, "LV2": 2, "LV3": 3}.get(lv, 0)

    items = list(_alert_buf)  # 최신순(appendleft)

    if level_min:
        minv = lv_rank(level_min.upper())
        items = [x for x in items if lv_rank(x["level"]) >= minv]

    if index:
        items = [x for x in items if x["index"] == index]

    if since:
        items = [x for x in items if x["triggered_at"] >= since]

    return {"items": items[:limit]}

# Procfile:
# web: uvicorn main:app --host 0.0.0.0 --port $PORT
