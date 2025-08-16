# main.py  (Assistants API v2, thread 고정 + inbox 지원)
import os, time, logging, requests
from typing import Optional
from collections import deque
from fastapi import FastAPI, Header, HTTPException, Request, Query

APP_VERSION = "sentinel-fastapi-v2-1.2.0"

# ── 환경변수 ──────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ASSISTANT_ID      = os.getenv("CAIA_ASSISTANT_ID", "")   # v2 Assistant ID
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
SENTINEL_KEY      = os.getenv("SENTINEL_KEY", "")        # (선택) POST 보호용 공유키
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()
DEDUP_WINDOW_MIN  = int(os.getenv("DEDUP_WINDOW_MIN", "30"))
ALERT_CAP         = int(os.getenv("ALERT_CAP", "2000"))  # 링버퍼 크기

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("sentinel-fastapi-v2")

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

# ── 외부 전송 함수들 ─────────────────────────────────────────────────
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

def send_caia_v2(text: str) -> bool:
    """
    Assistants API v2
    - 호출 시점마다 CAIA_THREAD_ID를 환경변수에서 재읽음(재배포 없이 교체 가능)
    - CAIA_THREAD_ID가 있으면 해당 스레드에 메시지 추가 + Run 실행
    - 없거나 404 등 오류 시, 새 Thread 생성하여 메시지 + Run (옵션 폴백)
      * 주의: 새 Thread는 ChatGPT 메인창과 다른 API Thread임
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
        # 1) Thread 결정 (매 호출마다 최신값 재읽기)
        thread_id_env = os.getenv("CAIA_THREAD_ID", "").strip()
        thread_id = None

        if thread_id_env:
            thread_id = thread_id_env
            log.info("기존 Thread 사용: %s", thread_id)

            # 2) 메시지 추가
            r1 = requests.post(
                f"{base}/threads/{thread_id}/messages",
                headers=headers,
                json={"role": "user", "content": text},
                timeout=10,
            )
            if r1.status_code == 404:
                log.error("지정 Thread 404 → 새 Thread로 폴백 시도")
                thread_id = None  # 폴백 경로로 전환
            elif not r1.ok:
                log.error("Message 추가 실패 %s %s", r1.status_code, r1.text)
                return False
            else:
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

        # (폴백) 새 Thread 생성 경로
        tr = requests.post(f"{base}/threads", headers=headers, timeout=8)
        if not tr.ok:
            log.error("Thread 생성 실패 %s %s", tr.status_code, tr.text)
            return False
        thread_id = tr.json().get("id", "")
        log.info("새 Thread 생성: %s", thread_id)

        r1 = requests.post(
            f"{base}/threads/{thread_id}/messages",
            headers=headers,
            json={"role": "user", "content": text},
            timeout=10,
        )
        if not r1.ok:
            log.error("Message 추가 실패 %s %s", r1.status_code, r1.text)
            return False

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

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="Sentinel FastAPI v2", version=APP_VERSION)

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
    msg = f"📡 [{data['level'].upper()}] {data['index']} {delta:+.2f}%"
    if covix is not None:
        try:
            msg += f" / COVIX {float(covix):+.2f}"
        except Exception:
            msg += f" / COVIX {covix}"
    msg += f" / ⏱ {data['triggered_at']}"
    if data.get("note"):
        msg += f" / 📝 {data['note']}"
    return msg

def _append_inbox(data: dict) -> None:
    """알림 원본을 정규화해 최신순으로 버퍼에 저장."""
    item = {
        "index":        str(data["index"]),
        "level":        str(data["level"]).upper(),
        "delta_pct":    float(data["delta_pct"]),
        "covix":        (None if data.get("covix") in (None, "") else float(str(data.get("covix")).replace("+","")) if str(data.get("covix")).replace(".","",1).lstrip("+-").isdigit() else data.get("covix")),
        "triggered_at": str(data["triggered_at"]),
        "note":         (data.get("note") or None),
    }
    _alert_buf.appendleft(item)

@app.post("/sentinel/alert")
async def sentinel_alert(
    request: Request,
    x_sentinel_key: Optional[str] = Header(default=None)
):
    # (선택) 공유키 검증
    if SENTINEL_KEY:
        if x_sentinel_key != SENTINEL_KEY:
            raise HTTPException(status_code=401, detail="invalid sentinel key")

    data = await request.json()

    # 필수 필드 검증
    for f in ("index", "level", "delta_pct", "triggered_at"):
        if f not in data:
            raise HTTPException(status_code=400, detail=f"missing field {f}")

    lvl = str(data["level"]).upper()
    if lvl not in {"LV1", "LV2", "LV3"}:
        raise HTTPException(status_code=400, detail="level must be LV1/LV2/LV3")

    idx = str(data["index"])

    # 중복 억제
    if within_dedup(idx, lvl):
        log.info("dedup: %s %s", idx, lvl)
        # 중복이어도 inbox에는 적재(선택 가능). 여기선 억제: 미적재.
        return {"status": "dedup_suppressed"}

    # 메시지 생성
    data["level"] = lvl
    msg = _format_msg(data)

    # 텔레그램 우선 전송
    tg_ok = send_telegram(msg)

    # 카이아(Assistants v2) 전송 시도
    caia_ok = send_caia_v2(msg)

    # inbox 적재(단기 보관)
    _append_inbox(data)

    return {"status": "delivered", "telegram": tg_ok, "caia_thread": caia_ok}

@app.get("/sentinel/inbox")
def sentinel_inbox(
    limit: int = Query(10, ge=1, le=50),
    level_min: Optional[str] = Query(None, pattern=r"^LV[1-3]$"),
    index: Optional[str] = None,
    since: Optional[str] = None,  # ISO8601 문자열(형식 동일 가정 시 문자열 비교도 가능)
):
    """커스텀 GPT Action이 호출하는 읽기 전용 API (단기 메모리)."""
    def lv_rank(lv: str) -> int:
        return {"LV1": 1, "LV2": 2, "LV3": 3}.get(lv, 0)

    items = list(_alert_buf)  # 이미 최신순(appendleft)

    if level_min:
        minv = lv_rank(level_min.upper())
        items = [x for x in items if lv_rank(x["level"]) >= minv]

    if index:
        items = [x for x in items if x["index"] == index]

    if since:
        # ISO8601 형식이 동일하다는 가정하에 문자열 비교로 필터
        items = [x for x in items if x["triggered_at"] >= since]

    return {"items": items[:limit]}

# Procfile:
# web: uvicorn main:app --host 0.0.0.0 --port $PORT
