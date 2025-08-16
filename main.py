# main.py
import os, time, logging, requests
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

APP_VERSION = "sentinel-fastapi-1.0.1"

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

# ── (참고) 원래 모델: 남겨두되 엔드포인트에서는 직접 파싱을 사용 ───────────
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

def _fmt_message_obj(index: str, level: str, delta_pct: float,
                     triggered_at: str, covix: Optional[float] = None, note: Optional[str] = None) -> str:
    parts = [f"📡 [Sentinel] {level} 감지 — {index} {delta_pct:+.2f}%"]
    if covix is not None:
        try:
            parts.append(f"COVIX {float(covix):+.2f}")
        except Exception:
            pass
    parts.append(f"⏱ {triggered_at}")
    if note:
        parts.append(f"📝 {note}")
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

# 🔍 진단용: 보낸 바디를 그대로 돌려줌
@app.post("/echo")
async def echo(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        raw = await request.body()
        log.error("ECHO json parse error: %s / raw=%r", e, raw[:500])
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return {"received": data}

# ✅ 유연 파서 버전: 어떤 JSON이 와도 먼저 받아서 우리가 검증/포맷
@app.post("/sentinel/alert")
async def sentinel_alert(request: Request, x_sentinel_key: Optional[str] = Header(default=None)):
    # (선택) 공유키 검증
    if SENTINEL_KEY and x_sentinel_key != SENTINEL_KEY:
        raise HTTPException(status_code=401, detail="invalid sentinel key")

    # 1) JSON 원문 수신
    try:
        data = await request.json()
    except Exception as e:
        raw = await request.body()
        log.error("JSON parse error: %s / raw=%r", e, raw[:500])
        raise HTTPException(status_code=400, detail="invalid JSON body")

    # 2) 필수 필드 검증 (수동)
    req_fields = ["source", "index", "level", "delta_pct", "triggered_at"]
    missing = [k for k in req_fields if k not in data]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing fields: {missing}")

    # level 정규화
    level = str(data["level"]).upper().strip()
    if level not in {"LV1", "LV2", "LV3"}:
        raise HTTPException(status_code=400, detail="level must be LV1/LV2/LV3")

    # 숫자 필드 보정
    try:
        delta = float(data["delta_pct"])
    except Exception:
        raise HTTPException(status_code=400, detail="delta_pct must be a number")

    covix = None
    if "covix" in data and data["covix"] is not None:
        try:
            covix = float(data["covix"])
        except Exception:
            raise HTTPException(status_code=400, detail="covix must be a number if provided")

    index = str(data["index"]).strip()
    trig_at = str(data["triggered_at"]).strip()
    note = str(data.get("note", "")).strip() or None

    # 3) dedup
    if _within_dedup(index, level):
        log.info("dedup: %s %s", index, level)
        return {"status": "dedup_suppressed"}

    # 4) 메시지 포맷 후 전송
    msg = _fmt_message_obj(index=index, level=level, delta_pct=delta, covix=covix, triggered_at=trig_at, note=note)
    tg_ok = send_telegram(msg)
    oa_ok = send_to_caia_thread(msg)

    return {"status": "delivered", "telegram": tg_ok, "caia_thread": oa_ok}

# 참고: 프로덕션에서 uvicorn은 Procfile/Start Command로 기동
# 로컬 테스트 시: uvicorn main:app --reload --port 8787
