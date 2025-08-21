"""
existential_server.py - 카이아 존재형 유산화 기억 시스템 서버
Server for Caia's Existential Legacy Memory System
"""

from __future__ import annotations
import os
import sys
import time
import threading
import asyncio
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

# 환경변수 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# 기존 시스템
try:
    from memory_manager import (
        get_shared_memory,
        ensure_qdrant_collection,
        QDRANT_COLLECTION,
        make_qdrant,
    )
    LEGACY_SYSTEM_AVAILABLE = True
except Exception as e:
    print(f"❌ memory_manager import 실패: {e}", file=sys.stderr)
    LEGACY_SYSTEM_AVAILABLE = False

# 존재형 기억 시스템
try:
    from existential_memory_integration import (
        get_existential_memory,
        memory_insert_experience,
        memory_train_experience,
        memory_retrieve_with_filter
    )
    from memory_schema import MemoryType, Actor, Importance, MemorySource
    EXISTENTIAL_MEMORY_AVAILABLE = True
except Exception as e:
    print(f"⚠️ Existential memory import 실패: {e}", file=sys.stderr)
    EXISTENTIAL_MEMORY_AVAILABLE = False

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("existential.server")

# FastAPI 앱
APP_START_TS = time.time()
APP_VERSION = "2025-12-18-existential"
app = FastAPI(
    title="Caia Existential Memory Server",
    version=APP_VERSION,
    description="카이아 존재형 유산화 기억 시스템"
)

# 전역 변수
existential_memory = None
scheduler_started = threading.Event()

# ========== 스타트업 이벤트 ==========

@app.on_event("startup")
async def on_startup():
    """서버 시작시 초기화"""
    global existential_memory
    
    # 1. Qdrant 초기화
    if LEGACY_SYSTEM_AVAILABLE:
        try:
            qc = make_qdrant()
            ensure_qdrant_collection(qc, QDRANT_COLLECTION)
            logger.info(f"✅ Qdrant ready: collection={QDRANT_COLLECTION}")
        except Exception as e:
            logger.error(f"⚠️ Qdrant 초기화 실패: {e}")
    
    # 2. 존재형 기억 시스템 초기화
    if EXISTENTIAL_MEMORY_AVAILABLE:
        try:
            existential_memory = get_existential_memory()
            logger.info("✅ 존재형 유산화 기억 시스템 초기화 완료")
        except Exception as e:
            logger.error(f"⚠️ 존재형 기억 시스템 초기화 실패: {e}")
    
    # 3. 스케줄러 시작 (옵션)
    if os.getenv("SCHED_AUTOSTART", "1") == "1":
        start_scheduler_background()

# ========== 헬스체크 엔드포인트 ==========

@app.get("/", response_class=PlainTextResponse)
def root():
    return "Caia Existential Memory Server"

@app.get("/health")
def health():
    """시스템 상태 확인"""
    status = {
        "status": "healthy",
        "uptime": round(time.time() - APP_START_TS, 2),
        "version": APP_VERSION,
        "systems": {
            "legacy": LEGACY_SYSTEM_AVAILABLE,
            "existential": EXISTENTIAL_MEMORY_AVAILABLE and existential_memory is not None,
            "scheduler": scheduler_started.is_set()
        }
    }
    return JSONResponse(status)

@app.get("/ready")
def ready():
    """준비 상태 확인"""
    if not existential_memory:
        return JSONResponse({"status": "not_ready", "reason": "Memory system not initialized"}, status_code=503)
    return JSONResponse({"status": "ready"})

# ========== 메모리 입력 엔드포인트 ==========

class RememberIn(BaseModel):
    """수동 기억 요청"""
    content: str = Field(description="기억할 내용")
    actor: Optional[str] = Field(None, description="행위자 (동현/Caia)")
    context: Optional[str] = Field(None, description="맥락 정보")

@app.post("/memory/remember")
async def remember(body: RememberIn):
    """
    수동 기억 처리 - "카이아 이거 기억해" 명령 처리
    """
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    result = existential_memory.remember(
        content=body.content,
        actor=body.actor,
        context=body.context
    )
    
    return JSONResponse(result)

class ExperienceIn(BaseModel):
    """경험 기억 입력"""
    content: str
    type: str = "strategy"
    actor: str = "Caia"
    importance: str = "medium"
    source: str = "manual"
    context: Optional[str] = None
    topics: Optional[List[str]] = None
    emotions: Optional[List[str]] = None

@app.post("/memory/experience")
async def insert_experience(body: ExperienceIn):
    """
    경험 기억 직접 삽입
    
    Types: strategy, feedback, existence, interaction, emotion, legacy, learning, failure, success, reflection
    Actors: 동현, Caia, System, Both
    Importance: critical, high, medium, low
    Sources: manual, memory_loop, realtime, reflection_loop, legacy_process
    """
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    result = existential_memory.insert_experience(
        content=body.content,
        memory_type=body.type,
        actor=body.actor,
        importance=body.importance,
        source=body.source,
        context=body.context,
        topics=body.topics,
        emotions=body.emotions
    )
    
    return JSONResponse(result)

class DialogueIn(BaseModel):
    """대화 입력"""
    messages: List[Dict[str, Any]]
    auto_legacy: Optional[bool] = None

@app.post("/memory/dialogue")
async def process_dialogue(body: DialogueIn, background_tasks: BackgroundTasks):
    """
    대화 처리 - 자동 기억 감지 및 유산화
    """
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    # 백그라운드에서 처리
    background_tasks.add_task(
        process_dialogue_background,
        messages=body.messages,
        auto_legacy=body.auto_legacy
    )
    
    return JSONResponse({"status": "processing", "message_count": len(body.messages)})

async def process_dialogue_background(messages: List[Dict], auto_legacy: bool = None):
    """백그라운드 대화 처리"""
    try:
        result = existential_memory.process_dialogue(messages, auto_legacy)
        logger.info(f"대화 처리 완료: {result}")
    except Exception as e:
        logger.error(f"대화 처리 실패: {e}")

# ========== 메모리 검색 엔드포인트 ==========

class RecallIn(BaseModel):
    """기억 회상 요청"""
    query: Optional[str] = None
    filter_type: Optional[str] = None
    actor: Optional[str] = None
    topics: Optional[List[str]] = None
    limit: int = 10
    include_legacy: bool = True

@app.post("/memory/recall")
async def recall(body: RecallIn):
    """
    기억 회상 - 필터 기반 검색
    
    filter_type: strategy, feedback, existence, interaction, emotion, legacy, learning, failure, success, reflection
    actor: 동현, Caia, System, Both
    """
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    results = existential_memory.recall(
        query=body.query,
        filter_type=body.filter_type,
        actor=body.actor,
        topics=body.topics,
        limit=body.limit,
        include_legacy=body.include_legacy
    )
    
    return JSONResponse({"memories": results, "count": len(results)})

@app.get("/memory/feedbacks")
async def get_feedbacks(limit: int = 10):
    """동현의 피드백 기억 조회"""
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    feedbacks = existential_memory.get_feedbacks(limit)
    return JSONResponse({"feedbacks": feedbacks, "count": len(feedbacks)})

@app.get("/memory/existence")
async def get_existence(limit: int = 10):
    """카이아의 존재 선언 조회"""
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    existences = existential_memory.get_existence_declarations(limit)
    return JSONResponse({"existences": existences, "count": len(existences)})

@app.get("/memory/interactions")
async def get_interactions(limit: int = 10):
    """상호작용 기억 조회"""
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    interactions = existential_memory.get_interactions(limit)
    return JSONResponse({"interactions": interactions, "count": len(interactions)})

@app.get("/memory/legacies")
async def get_legacies(limit: int = 10):
    """유산화된 핵심 기억 조회"""
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    legacies = existential_memory.get_legacies(limit)
    return JSONResponse({"legacies": legacies, "count": len(legacies)})

# ========== 학습 및 분석 엔드포인트 ==========

class TrainIn(BaseModel):
    """학습 요청"""
    experiences: Optional[List[Dict[str, Any]]] = None
    time_window_hours: int = 24

@app.post("/memory/train")
async def train(body: TrainIn):
    """
    기억 학습 - 패턴 분석 및 유산화
    """
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    result = existential_memory.train(
        experiences=body.experiences,
        time_window_hours=body.time_window_hours
    )
    
    return JSONResponse(result)

@app.get("/memory/analyze")
async def analyze():
    """전체 기억 시스템 분석"""
    if not existential_memory:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    
    analysis = existential_memory.analyze()
    return JSONResponse(analysis)

# ========== 기존 API 호환 엔드포인트 ==========

class EchoIn(BaseModel):
    """기존 echo API 호환"""
    messages: List[Dict[str, Any]]

@app.post("/memory/echo")
async def memory_echo(body: EchoIn, background_tasks: BackgroundTasks):
    """기존 echo API 호환 - 자동으로 존재형 시스템으로 전달"""
    
    # 기존 시스템 처리
    if LEGACY_SYSTEM_AVAILABLE:
        mem = get_shared_memory()
        for m in body.messages:
            mem.echo({
                "type": m.get("type", "human"),
                "content": m.get("content", ""),
                "topic": m.get("topic", []),
                "timestamp": datetime.utcnow().isoformat()
            })
    
    # 존재형 시스템으로 전달
    if existential_memory:
        background_tasks.add_task(
            process_dialogue_background,
            messages=body.messages
        )
    
    return JSONResponse({"status": "processed", "count": len(body.messages)})

class RetrieveIn(BaseModel):
    """기존 retrieve API 호환"""
    query: str
    top_k: int = 5
    topics_any: Optional[List[str]] = None

@app.post("/memory/retrieve")
async def memory_retrieve(body: RetrieveIn):
    """기존 retrieve API 호환"""
    
    if existential_memory:
        # 존재형 시스템 사용
        results = existential_memory.recall(
            query=body.query,
            topics=body.topics_any,
            limit=body.top_k
        )
        return JSONResponse({"recalled": [r["content"] for r in results], "hits": results})
    elif LEGACY_SYSTEM_AVAILABLE:
        # 기존 시스템 폴백
        mem = get_shared_memory()
        hits = mem.vector_search(body.query, top_k=body.top_k, topics_any=body.topics_any)
        recalled = [h.get("text", "") for h in hits if h.get("text")]
        return JSONResponse({"recalled": recalled, "hits": hits})
    else:
        raise HTTPException(status_code=503, detail="No memory system available")

# ========== 스케줄러 ==========

def start_scheduler_background():
    """백그라운드 스케줄러 시작"""
    if scheduler_started.is_set():
        return
    
    def run_scheduler():
        try:
            # 스케줄러 임포트 및 실행
            from enhanced_scheduler import start_enhanced_scheduler
            start_enhanced_scheduler()
        except Exception as e:
            logger.error(f"스케줄러 실행 실패: {e}")
    
    thread = threading.Thread(target=run_scheduler, daemon=True, name="existential-scheduler")
    thread.start()
    scheduler_started.set()
    logger.info("🕒 Existential scheduler started")

# ========== 진단 엔드포인트 ==========

@app.get("/diag/env")
def diag_env():
    """환경 진단"""
    
    analysis = None
    if existential_memory:
        try:
            analysis = existential_memory.analyze()
        except:
            pass
    
    return JSONResponse({
        "version": APP_VERSION,
        "uptime": round(time.time() - APP_START_TS, 2),
        "systems": {
            "legacy": LEGACY_SYSTEM_AVAILABLE,
            "existential": EXISTENTIAL_MEMORY_AVAILABLE,
            "memory_initialized": existential_memory is not None,
            "scheduler": scheduler_started.is_set()
        },
        "memory_analysis": analysis,
        "env": {
            "OPENAI_API_KEY": bool(os.getenv("OPENAI_API_KEY")),
            "QDRANT_URL": bool(os.getenv("QDRANT_URL")),
            "QDRANT_API_KEY": bool(os.getenv("QDRANT_API_KEY")),
            "SCHED_AUTOSTART": os.getenv("SCHED_AUTOSTART", "1")
        }
    })

# ========== 메인 실행 ==========

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(
        "existential_server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )