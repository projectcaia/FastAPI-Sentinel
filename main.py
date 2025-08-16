# main.py
import os, time, logging, requests
from typing import Optional
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

APP_VERSION = "sentinel-fastapi-1.0.0"

# ── 환경변수 ─────────────────────────────────────────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
CAIA_THREAD_ID      = os.getenv("CAIA_THREAD_ID", "")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
SENTINEL_KEY        = os.getenv("SENTINEL_KEY", "")  # (선택) 호출 보호용 공유키
DEDUP_WINDOW_MIN    = int(os.getenv("DEDUP_WINDOW_MIN", "30"))  # 같은 index+level 중복 억제
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("sentinel-fastapi")

# ── 중복 억제 in-memory ──────────────────────────────────────────────
# key: (index, level) -> last_epoch_sec
_last_fired = {}

# ── 모델 ─────────────────────────────────────────────────────────────
class AlertPayload(BaseModel):
    source: str = Field(..., description="호출자 태그 예: sentinel")
    index: str = Field(..., description="지표명 예: ΔK200, KOSPI200, S&P500")
    level: str = Field(..., pattern=r"^LV[1-3]$", description="LV1/LV2/LV3")
    delta_pct: float = Field(..., description="지표 변화율(%) 예: -1.28")
    covix: Optional[float] = Field(None, description="변동성 보조지표(선택)")
    triggered_at: str = Field(..., description="ISO8601 시각 문자열(+09:00 권장)")
    note: Optional[str] = Field(None, description="추가 메모/대체지표 등")

# ── FastAPI ──────────────────────────────────────────────────────────
app = FastAPI(title="Sentinel FastAPI", version=APP_VERSION)

def _within_dedup(index: str, level: str) -> bool:
    """같은 index+level 이벤트가 dedup 윈도우 내면 True"""
    now = time.time()
    key = (index, level)
    last = _last_fired.get(key)
    if last and (now - last) < DEDUP_WINDOW_MIN * 60:
        return True
    _last_fired[key] = now
    return False

def _fmt_message(p: AlertPayload) -> str:
    parts = [f"📡 [Sentinel] {p.level} 감지 — {p.index} {p.delta_pct:+.2f}%"]
    if p.covix is not None:
        parts.append(f"COVIX {p.covix:+.2f}")
    parts.append(f"⏱ {p.triggered_at}")
    if p.note:
        parts.append(f"📝 {p.note}")
    return " / ".join(parts)

def send_telegram(text: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log.warning("Telegram env 미설정, 스킵")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if r.ok:
            return True
        log.error("Telegram 실패: %s %s", r.status_code, r.text)
        return False
    except Exception as e:
        log.exception("Telegram 예외: %s", e)
        return False

def send_to_caia_thread(text: str) -> bool:
    if not (OPENAI_API_KEY and CAIA_THREAD_ID):
        log.warning("OpenAI/Caia env 미설정, 스킵")
        return False
    url = f"https://api.openai.com/v1/threads/{CAIA_THREAD_ID}/messages"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v1"
    }
    try:
        r = requests.post(url, headers=headers, json={"role": "user", "content": text}, timeout=12)
        if r.ok:
            return True
        log.error("OpenAI Thread 메시지 실패: %s %s", r.status_code, r.text)
        return False
    except Exception as e:
        log.exception("OpenAI 예외: %s", e)
        return False

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "thread_set": bool(CAIA_THREAD_ID),
        "tg_set": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "dedup_window_min": DEDUP_WINDOW_MIN
    }

@app.post("/sentinel/alert")
def sentinel_alert(
    payload: AlertPayload,
    x_sentinel_key: Optional[str] = Header(default=None)
):
    # (선택) 공유키 검증
    if SENTINEL_KEY:
        if x_sentinel_key != SENTINEL_KEY:
            raise HTTPException(status_code=401, detail="invalid sentinel key")

    # 중복 억제
    if _within_dedup(payload.index, payload.level):
        log.info("dedup: %s %s", payload.index, payload.level)
        return {"status": "dedup_suppressed"}

    msg = _fmt_message(payload)
    # 우선순위: 텔레그램 → OpenAI (OpenAI 실패해도 텔레그램은 반드시 시도)
    tg_ok = send_telegram(msg)
    oa_ok = send_to_caia_thread(msg)
    return {"status": "delivered", "telegram": tg_ok, "caia_thread": oa_ok}

# 참고: 프로덕션에서 uvicorn은 Procfile로 기동
# 로컬 테스트 시: uvicorn main:app --reload --port 8787

