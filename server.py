# server.py — Caia Memory Server (Option A: Web + Scheduler in one process)
# EMBED_DIM import 제거판 (memory_manager에 EMBED_DIM가 없을 때도 안전)

from __future__ import annotations
import os
import sys
import time
import threading
from typing import Any, Dict, List, Optional

# (로컬 실행 시 .env 자동 로드 — 프로덕션에 영향 없음)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────
# 내부 모듈 의존 — EMBED_DIM import 삭제 (여기서 계산/표시)
# ─────────────────────────────────────────────────────────────
try:
    from memory_manager import (
        get_shared_memory,          # 동일 프로세스 공유 메모리 핸들러
        ensure_qdrant_collection,   # 컬렉션/인덱스 보장
        QDRANT_COLLECTION,
        make_qdrant,
    )
except Exception as e:  # noqa: BLE001
    print(f"❌ memory_manager import 실패: {type(e).__name__}: {e}", file=sys.stderr)
    raise

# (선택) OpenAI SDK — /memory/invoke 보조 요약에만 사용
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

APP_START_TS = time.time()
APP_VERSION  = "2025-08-18"
app = FastAPI(title="Caia Memory Server", version=APP_VERSION)

# 스케줄러 자동 기동 옵션
SCHED_AUTOSTART = os.getenv("SCHED_AUTOSTART", "1")
_sched_started_flag = threading.Event()

# 중요 ENV (진단 노출용)
CAIA_IMPORTANCE_BASE = os.getenv("CAIA_IMPORTANCE_BASE", "0.5")
RAW_BACKUP_PATH      = os.getenv("RAW_BACKUP_PATH", "").strip()
PUBLIC_BASE_URL      = os.getenv("PUBLIC_BASE_URL", "")
FUNCTION_CALLING_URL = os.getenv("FUNCTION_CALLING_URL", "")

def _embed_dim_probe() -> Optional[int]:
    """
    공유 메모리에서 embed_dim을 얻고, 없으면 ENV EMBEDDING_DIM(기본 1536)로 대체.
    """
    try:
        mem = get_shared_memory()
        dim = getattr(mem, "embed_dim", None)
        if isinstance(dim, int) and dim > 0:
            return dim
    except Exception:
        pass
    try:
        return int(os.getenv("EMBEDDING_DIM", "1536"))
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# Scheduler 부트스트랩 (옵션 A 핵심)
# ─────────────────────────────────────────────────────────────
def _start_scheduler_background() -> None:
    if _sched_started_flag.is_set():
        return
    try:
        from scheduler import start_scheduler  # 지연 임포트(순환참조 회피)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ scheduler 임포트 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return

    def _run():
        try:
            start_scheduler()
        except Exception as e:  # noqa: BLE001
            print(f"❌ scheduler 종료: {type(e).__name__}: {e}", file=sys.stderr)

    t = threading.Thread(target=_run, daemon=True, name="caia-scheduler")
    t.start()
    _sched_started_flag.set()
    print("🕒 Scheduler thread started (daemon)")

# ─────────────────────────────────────────────────────────────
# FastAPI 스타트업 훅: Qdrant 보장 + 스케줄러 기동
# ─────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup() -> None:
    # 1) Qdrant 컬렉션/인덱스 보장 (embed dim은 memory_manager 내부에서 결정)
    try:
        qc = make_qdrant()
        ensure_qdrant_collection(qc, QDRANT_COLLECTION)
        print(f"✅ Qdrant ready: collection={QDRANT_COLLECTION}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Qdrant ensure 실패: {e}", file=sys.stderr)

    # 2) 스케줄러 자동 기동
    if SCHED_AUTOSTART.lower() in ("1", "true", "yes", "y"):
        _start_scheduler_background()
    else:
        print("ℹ️ SCHED_AUTOSTART=0 → scheduler autostart disabled")

# ─────────────────────────────────────────────────────────────
# 상태/진단 엔드포인트
# ─────────────────────────────────────────────────────────────
@app.get("/", response_class=PlainTextResponse)
def root() -> str:
    return "Caia Memory Server"

@app.get("/ready")
def ready() -> JSONResponse:
    return JSONResponse({
        "status": "ready",
        "uptime_sec": round(time.time() - APP_START_TS, 3),
        "scheduler_started": _sched_started_flag.is_set()
    })

@app.get("/health")
def health() -> JSONResponse:
    ok = True
    detail: Dict[str, Any] = {}
    try:
        qc = make_qdrant()
        _ = qc.get_collections()
        detail["qdrant"] = "ok"
    except Exception as e:  # noqa: BLE001
        ok = False
        detail["qdrant"] = f"error: {type(e).__name__}: {e}"
    return JSONResponse({"status": "ok" if ok else "error", "detail": detail})

@app.get("/diag/env")
def diag_env() -> JSONResponse:
    return JSONResponse({
        "version": APP_VERSION,
        "collection": QDRANT_COLLECTION,
        "embed_dim": _embed_dim_probe(),           # 🔹 여기서 안전하게 표시
        "scheduler_started": _sched_started_flag.is_set(),
        "env": {
            "CAIA_IMPORTANCE_BASE": CAIA_IMPORTANCE_BASE,
            "RAW_BACKUP_PATH_set": bool(RAW_BACKUP_PATH),
            "PUBLIC_BASE_URL_set": bool(PUBLIC_BASE_URL),
            "FUNCTION_CALLING_URL_set": bool(FUNCTION_CALLING_URL),
            "QDRANT_URL_set": bool(os.getenv("QDRANT_URL")),
            "QDRANT_API_KEY_set": bool(os.getenv("QDRANT_API_KEY")),
            "OPENAI_API_KEY_set": bool(os.getenv("OPENAI_API_KEY")),
            "SCHED_AUTOSTART": SCHED_AUTOSTART,
        },
    })

class DiagIn(BaseModel):
    collection: Optional[str] = None
    ensure_indexes: bool = True

@app.post("/diag/qdrant")
def diag_qdrant(body: DiagIn) -> JSONResponse:
    coll = body.collection or QDRANT_COLLECTION
    try:
        qc = make_qdrant()
        created = ensure_qdrant_collection(qc, coll, ensure_indexes=body.ensure_indexes)
        return JSONResponse({"status": "ok", "collection": coll, "created": created, "embed_dim": _embed_dim_probe()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

# ─────────────────────────────────────────────────────────────
# 메모리 API
# ─────────────────────────────────────────────────────────────
class Message(BaseModel):
    type: str = Field(default="human")  # human/ai/system
    content: str
    topic: Optional[List[str]] = None
    level: Optional[str] = "L2"

class EchoIn(BaseModel):
    messages: List[Message]

@app.post("/memory/echo")
def memory_echo(body: EchoIn) -> JSONResponse:
    mem = get_shared_memory()
    out = []
    for m in body.messages:
        data = {
            "type": (m.type or "human").lower(),
            "content": m.content or "",
            "topic": (m.topic or []),
            "level": (m.level or "L2"),
            "timestamp": mem.now_iso(),
        }
        mem.echo(data)
        out.append({
            "type": data["type"],
            "content": data["content"],
            "topic": data["topic"],
            "level": data["level"],
        })
    return JSONResponse(out)

class RetrieveIn(BaseModel):
    query: str
    top_k: int = 5
    topics_any: Optional[List[str]] = None

@app.post("/memory/retrieve")
def memory_retrieve(body: RetrieveIn) -> JSONResponse:
    mem = get_shared_memory()
    hits = mem.vector_search(body.query, top_k=max(1, body.top_k), topics_any=body.topics_any)
    recalled = [h.get("text", "") for h in hits if h.get("text")]
    return JSONResponse({"recalled": recalled, "hits": hits})

class InvokeIn(BaseModel):
    messages: Optional[List[Message]] = None

@app.post("/memory/invoke")
def memory_invoke(body: InvokeIn) -> JSONResponse:
    mem = get_shared_memory()

    # 간이 요약(모델 없이 라인 압축)
    lines: List[str] = []
    for m in (body.messages or [])[-10:]:
        role = (m.type or "human").strip()
        content = (m.content or "").strip().replace("\n", " ")
        if content:
            lines.append(f"- ({role}) {content[:200]}")
        if len(lines) >= 5:
            break
    summary = "\n".join(lines)

    if summary.strip():
        try:
            mem.add_vector_memory(summary, metadata={
                "level": "L2",
                "topic": "summary",
                "tags": ["invoke"],
                "ts": mem.now_iso(),
            })
        except Exception:
            pass

    return JSONResponse({"status": "invoked", "ok": True, "summary_len": len(summary)})

# ─────────────────────────────────────────────────────────────
# 로컬 직접 실행
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    if SCHED_AUTOSTART.lower() in ("1", "true", "yes", "y"):
        _start_scheduler_background()
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False, log_level="info")
