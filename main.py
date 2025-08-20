# main.py  (Assistants API v2, thread 고정 + inbox 지원, debug 강화)
import os
import time
import logging
import requests
from typing import Optional, Dict, Any
from collections import deque
from fastapi import FastAPI, Header, HTTPException, Request, Query

APP_VERSION = "sentinel-fastapi-v2-1.2.3"

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="Sentinel FastAPI v2", version=APP_VERSION)

# ── 환경변수 ──────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID      = os.getenv("CAIA_ASSISTANT_ID", "")   # v2 Assistant ID
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "")
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
log.info("  DEDUP_WINDOW_MIN: %d", DEDUP_WINDOW_MIN)
log.info("  ALERT_CAP: %d", ALERT_CAP)
log.info("=" * 60)

# ── 중복 억제 및 링버퍼 ──────────────────────────────────────────────
_last_fired = {}                        # key=(index,level) -> epoch
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

# ── 외부 전송: Caia(Assistants v2) ───────────────────────────────────
def _oai_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }

def send_caia_v2(text: str) -> Dict[str, Any]:
    """
    Assistants API v2
    - Run 생성 시 tool_choice=function(getLatestAlerts) **강제**
    - 실패/예외도 dict로 반환 (서버는 500으로 올리지 않음)
    """
    if not (OPENAI_API_KEY and ASSISTANT_ID):
        return {"ok": False, "stage": "precheck", "reason": "OPENAI/ASSISTANT env not set"}

    base = "https://api.openai.com/v1"
    headers = _oai_headers()

    try:
        thread_id = os.getenv("CAIA_THREAD_ID", "").strip()
        if not thread_id:
            return {"ok": False, "stage": "precheck", "reason": "CAIA_THREAD_ID not set"}

        # 1) 메시지 추가
        r1 = requests.post(
            f"{base}/threads/{thread_id}/messages",
            headers=headers,
            json={"role": "user", "content": text},
            timeout=12,
        )
        if r1.status_code == 404:
            return {"ok": False, "stage": "message", "status": 404, "resp": r1.text, "thread_id": thread_id}
        if not r1.ok:
            return {"ok": False, "stage": "message", "status": r1.status_code, "resp": r1.text, "thread_id": thread_id}

        # 2) Run 생성: 툴콜 강제
        run_body = {
            "assistant_id": ASSISTANT_ID,
            "tool_choice": {
                "type": "function",
                "function": {"name": "getLatestAlerts"}  # operationId와 정확히 일치
            },
            "instructions": (
                "센티넬/알람 키워드 감지. getLatestAlerts(limit=10 기본)을 호출해 최근 알림을 요약하라. "
                "응답 형식: (지표, 레벨, Δ%, 시각, note) 한 줄 리스트. "
                "전략 판단은 지금 하지 말고, 마지막에 '전략 판단 들어갈까요?'만 질문."
            ),
        }
        r2 = requests.post(
            f"{base}/threads/{thread_id}/runs",
            headers=headers,
            json=run_body,
            timeout=15,
        )
        if not r2.ok:
            return {"ok": False, "stage": "run", "status": r2.status_code, "resp": r2.text, "thread_id": thread_id}

        run_id = r2.json().get("id", "")
        return {"ok": True, "thread_id": thread_id, "run_id": run_id}

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
    - 절대 예외로 500 올리지 않음 (항상 200 JSON 반환)
    - 실패해도 stage/status/resp(reason) 등 디테일 제공
    """
    try:
        # (선택) 공유키 검증
        if SENTINEL_KEY and x_sentinel_key != SENTINEL_KEY:
            return {"status": "error", "where": "auth", "detail": "invalid sentinel key"}

        data = await request.json()

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
            return {
                "status": "dedup_suppressed",
                "reason": f"same alert within {DEDUP_WINDOW_MIN} minutes"
            }

        # 메시지 생성
        data["level"] = lvl
        msg = _format_msg(data)

        log.info("알림 전송: %s %s %.2f%% - %s",
                 idx, lvl, float(data.get("delta_pct", 0)),
                 data.get("note", ""))

        # 텔레그램
        tg_ok = send_telegram(msg)

        # 카이아(Assistants v2) — 툴콜 강제 + 디버그
        caia_info = send_caia_v2(msg)

        # inbox 적재
        _append_inbox(data)

        return {
            "status": "delivered",
            "telegram": tg_ok,
            "caia": caia_info,   # ← 여기서 성공/실패/사유가 바로 보임
            "message": msg
        }

    except Exception as e:
        # 마지막 방어: 그래도 200으로 에러 정보만 반환
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
        # ISO8601 형식 동일 가정하에 문자열 비교
        items = [x for x in items if x["triggered_at"] >= since]

    return {"items": items[:limit]}

# Procfile:
# web: uvicorn main:app --host 0.0.0.0 --port $PORT
