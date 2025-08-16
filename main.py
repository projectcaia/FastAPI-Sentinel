# main.py  (Assistants API v2, thread 고정 지원)
import os, time, logging, requests
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request

APP_VERSION = "sentinel-fastapi-v2-1.1.0"

# ── 환경변수 ───────────────────────────────────────────────
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID     = os.getenv("CAIA_ASSISTANT_ID", "")  # v2 Assistant ID
CAIA_THREAD_ID   = os.getenv("CAIA_THREAD_ID", "")     # ★ 메인 대화창 Thread ID (있으면 그걸로 전송)
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
DEDUP_WINDOW_MIN = int(os.getenv("DEDUP_WINDOW_MIN", "30"))

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("sentinel-fastapi-v2")

# ── 중복 억제 ───────────────────────────────────────────────
_last = {}  # key=(index,level) -> epoch

def within_dedup(idx, lvl):
    now = time.time()
    k = (idx, lvl)
    if k in _last and now - _last[k] < DEDUP_WINDOW_MIN * 60:
        return True
    _last[k] = now
    return False

# ── 외부 전송 함수 ──────────────────────────────────────────
def send_telegram(text: str):
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

def send_caia_v2(text: str):
    """
    v2 Assistants API
    - CAIA_THREAD_ID가 있으면 그 스레드에 직접 메시지 추가 + Run 실행
    - 없으면 새 스레드 생성 후 메시지 추가 + Run 실행
    """
    if not (OPENAI_API_KEY and ASSISTANT_ID):
        log.warning("OpenAI env 미설정 → 스킵")
        return False

    base = "https://api.openai.com/v1"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2",
    }

    try:
        # 1) Thread 결정
        if CAIA_THREAD_ID:
            thread_id = CAIA_THREAD_ID
            log.info("기존 Thread 사용: %s", thread_id)
        else:
            tr = requests.post(f"{base}/threads", headers=headers, timeout=8)
            if not tr.ok:
                log.error("Thread 생성 실패 %s %s", tr.status_code, tr.text)
                return False
            thread_id = tr.json().get("id", "")
            log.info("새 Thread 생성: %s", thread_id)

        # 2) 메시지 추가
        r1 = requests.post(
            f"{base}/threads/{thread_id}/messages",
            headers=headers,
            json={"role": "user", "content": text},
            timeout=8,
        )
        if not r1.ok:
            log.error("Message 추가 실패 %s %s", r1.status_code, r1.text)
            return False

        # 3) Run 실행
        r2 = requests.post(
            f"{base}/threads/{thread_id}/runs",
            headers=headers,
            json={"assistant_id": ASSISTANT_ID},
            timeout=12,
        )
        if not r2.ok:
            log.error("Run 실행 실패 %s %s", r2.status_code, r2.text)
            return False

        return True

    except Exception as e:
        log.exception("OpenAI 예외: %s", e)
        return False

# ── FastAPI ────────────────────────────────────────────────
app = FastAPI(title="Sentinel FastAPI v2", version=APP_VERSION)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "assistant_set": bool(ASSISTANT_ID),
        "tg_set": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "thread_fixed": bool(CAIA_THREAD_ID),
        "dedup_min": DEDUP_WINDOW_MIN,
    }

@app.post("/sentinel/alert")
async def sentinel_alert(request: Request):
    data = await request.json()

    # 필수 필드 검증
    for f in ("index", "level", "delta_pct", "triggered_at"):
        if f not in data:
            raise HTTPException(status_code=400, detail=f"missing field {f}")

    idx, lvl = data["index"], str(data["level"]).upper()
    if lvl not in {"LV1", "LV2", "LV3"}:
        raise HTTPException(status_code=400, detail="level must be LV1/LV2/LV3")

    if within_dedup(idx, lvl):
        return {"status": "dedup_suppressed"}

    # 메시지 포맷
    delta = float(data["delta_pct"])
    covix = data.get("covix")
    msg = f"📡 [{lvl}] {idx} {delta:+.2f}%"
    if covix is not None:
        try:
            msg += f" / COVIX {float(covix):+.2f}"
        except Exception:
            msg += f" / COVIX {covix}"
    msg += f" / ⏱ {data['triggered_at']}"
    if note := data.get("note"):
        msg += f" / 📝 {note}"

    tg_ok = send_telegram(msg)
    caia_ok = send_caia_v2(msg)

    return {"status": "delivered", "telegram": tg_ok, "caia_thread": caia_ok}

# Procfile: web: uvicorn main:app --host 0.0.0.0 --port $PORT
