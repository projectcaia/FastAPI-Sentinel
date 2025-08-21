# memory_manager.py — Caia Memory Layer (Option A compatible / v2.4, KST 2025-08-18)
# 목적
#   - RAW 우선(append-only) 기록 + 선택적 JSONL 백업
#   - Qdrant 벡터스토어 연결/보장 + 벡터 추가/검색 헬퍼
#   - 서버/스케줄러 동일 프로세스에서 공유 싱글턴 제공(get_shared_memory)
# 변경점(v2.4)
#   - EMBED_DIM 전역 export (기본 ENV/FALLBACK → 벡터스토어 초기화 시 실제 값으로 갱신)
#   - Qdrant 검색 토픽 필터를 filter(qmodels.Filter)로 정정
#   - 내구성 로그/예외 처리 보강
#
# ENV
#   OPENAI_API_KEY(선택), QDRANT_URL, QDRANT_API_KEY, QDRANT_COLLECTION(=caia-memory)
#   EMBEDDING_MODEL=text-embedding-3-small, EMBEDDING_DIM=1536
#   CAIA_IMPORTANCE_BASE=0.5
#   RAW_BACKUP_PATH=storage/raw.jsonl
#   RAW_TTL_DAYS=7, RAW_MAX_ITEMS=5000
#   SAFE_BOOT=0 (1이면 벡터스토어 초기화 생략)

from __future__ import annotations

import os
from typing import List, Dict, Any, Optional, Iterable, Tuple
from urllib.parse import urlparse
from datetime import datetime

# LangChain 메모리/벡터스토어
from langchain.memory import ConversationBufferMemory
from langchain.schema import HumanMessage, AIMessage
from langchain.schema.runnable import RunnableLambda
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Qdrant as LC_Qdrant

# Qdrant 클라이언트
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

# ─────────────────────────────────────────────
# 환경/백업/임계
# ─────────────────────────────────────────────
DEFAULT_EMBED_MODEL   = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
FALLBACK_EMBED_DIM    = int(os.getenv("EMBEDDING_DIM", "1536"))     # 모델 변경 내성
SAFE_BOOT             = os.getenv("SAFE_BOOT", "0") == "1"          # 부트 시 벡터스토어 스킵
RAW_BACKUP_PATH       = os.getenv("RAW_BACKUP_PATH", "").strip()    # 예: storage/raw.jsonl
RAW_TTL_DAYS          = int(os.getenv("RAW_TTL_DAYS", "7"))         # L0 TTL 기본 7일
RAW_MAX_ITEMS         = int(os.getenv("RAW_MAX_ITEMS", "5000"))     # 메모리 상한(안전장치)
CAIA_IMPORTANCE_BASE  = float(os.getenv("CAIA_IMPORTANCE_BASE", "0.50"))

QDRANT_COLLECTION     = os.getenv("QDRANT_COLLECTION", "caia-memory")

# ✅ 서버에서 import 가능한 전역 내보내기(기본값). 초기화 이후 실제 값으로 갱신됨.
EMBED_DIM: int = FALLBACK_EMBED_DIM

# 자연어 수동 학습/중요도 신호(ENV로 재정의 가능)
MANUAL_TRIGGERS = tuple(
    (os.getenv("CAIA_MANUAL_TRIGGERS",
               "기억,학습,적용,반영,저장,유산화,피드백,전략,판단,세션 정리,세션 마감,존재,진화,정체성,관계,추억"))
    .split(",")
)
DECISION_PATTERNS = tuple(
    (os.getenv("CAIA_DECISION_PATTERNS",
               "고정,변경,수정,철회,제거,허용,금지,임계,스케줄,목표,책임,마감,규칙,전략명,우선순위,선언,Codex,Seed3,Existential"))
    .split(",")
)

# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def _sanitize_env(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    for z in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        s = s.replace(z, "")
    return s

def _hex(s: str) -> str:
    return " ".join(f"{ord(c):02X}" for c in s)

def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def _append_jsonl(path: str, obj: dict):
    """옵션: Raw를 파일로도 남겨 재기동 복원/감사를 가능하게 함."""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[CaiaMemory] RAW 백업 실패: {e}")

def _get_embeddings_and_dim() -> Tuple[OpenAIEmbeddings, int]:
    """
    OpenAI 임베딩 객체와 차원수를 반환.
    쿼리 시 에러가 나면 FALLBACK_EMBED_DIM으로 대체.
    """
    emb = OpenAIEmbeddings(model=DEFAULT_EMBED_MODEL)
    try:
        dim = len(emb.embed_query("caia dim probe"))
    except Exception:
        dim = FALLBACK_EMBED_DIM
    return emb, dim

# ─────────────────────────────────────────────
# Qdrant 연결/보장 (server.py가 직접 호출)
# ─────────────────────────────────────────────
def make_qdrant() -> Optional[QdrantClient]:
    q_url_raw = os.getenv("QDRANT_URL", "")
    q_key_raw = os.getenv("QDRANT_API_KEY", "")
    q_url = _sanitize_env(q_url_raw)
    q_key = _sanitize_env(q_key_raw)

    if q_url and not q_url.startswith(("http://", "https://")):
        q_url = "https://" + q_url

    if q_url:
        parsed = urlparse(q_url)
        if not parsed.scheme or not parsed.netloc:
            print(f"[Qdrant] URL 형식 오류: {q_url}  (raw={repr(q_url_raw)}, hex={_hex(q_url_raw)})")
            return None

    if not q_url or not q_key:
        print("[Qdrant] 환경 미설정(QDRANT_URL/API_KEY)")
        return None

    try:
        client = QdrantClient(url=q_url, api_key=q_key, timeout=30.0)
        return client
    except Exception as e:
        print(f"[Qdrant] 클라이언트 생성 실패: {e}")
        return None


def ensure_qdrant_collection(
    client: Optional[QdrantClient],
    collection_name: str,
    *,
    ensure_indexes: bool = True
) -> bool:
    """
    컬렉션/인덱스 보장. 반환: created 여부.
    """
    if client is None:
        raise RuntimeError("qdrant_client_unavailable")

    created = False
    try:
        client.get_collection(collection_name)
    except Exception:
        # 없으면 생성
        emb, dim = _get_embeddings_and_dim()
        try:
            client.create_collection(
                collection_name=collection_name,
                vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
            )
            created = True
        except Exception as e_create:
            if "already exists" not in str(e_create) and getattr(e_create, "status_code", 0) != 409:
                raise

    if ensure_indexes:
        for field, schema in (("topic", "keyword"), ("level", "keyword"), ("ts_unix", "integer")):
            try:
                client.create_payload_index(collection_name, field_name=field, field_schema=schema)
            except Exception:
                pass

    return created


# ─────────────────────────────────────────────
# CaiaMemory (Raw 전용 + 벡터 헬퍼)
# ─────────────────────────────────────────────
class CaiaMemory(ConversationBufferMemory):
    """
    원칙:
    - 이 클래스는 **Raw 기록** 우선(append-only).
    - 즉시 임베딩은 제한적으로(add_vector_memory)만 사용. 배치(archive/train/legacy)에서 주로 활용.
    - history.messages(AIMessage)에 표준 메타(type/topic/tags/importance/timestamp/level/source_span) 부여.
    """
    def __init__(self, session_id: Optional[str] = None, *args, **kwargs):
        kwargs.setdefault("return_messages", True)
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "session_id", session_id)

        if SAFE_BOOT:
            object.__setattr__(self, "vectorstore", None)
            print("[CaiaMemory] SAFE_BOOT=1 – 벡터스토어 초기화 생략")
            return

        client = make_qdrant()
        if not client:
            object.__setattr__(self, "vectorstore", None)
            print("[CaiaMemory] Qdrant 미연결 – vectorstore 비활성")
            return

        # 컬렉션 보장
        try:
            ensure_qdrant_collection(client, QDRANT_COLLECTION)
        except Exception as e:
            object.__setattr__(self, "vectorstore", None)
            print(f"[CaiaMemory] Qdrant ensure 실패: {e}")
            return

        try:
            embeddings, dim = _get_embeddings_and_dim()
            vectorstore = LC_Qdrant(client=client, collection_name=QDRANT_COLLECTION, embeddings=embeddings)
            object.__setattr__(self, "vectorstore", vectorstore)
            object.__setattr__(self, "embed_dim", dim)
            # 🔹 전역 EMBED_DIM을 실제 값으로 갱신(export 유지)
            try:
                globals()["EMBED_DIM"] = int(dim)
            except Exception:
                pass
            print(f"[CaiaMemory] Qdrant 연결 성공 / collection={QDRANT_COLLECTION}, embed={DEFAULT_EMBED_MODEL}({dim})")
        except Exception as e:
            object.__setattr__(self, "vectorstore", None)
            print(f"[CaiaMemory] vectorstore 생성 실패: {e}")

    # 편의
    def now_iso(self) -> str:
        return _now_iso()

    @property
    def history(self):
        return self.chat_memory

    # ───────────────────── Raw Append API ───────────────────── #
    def echo(self, data: Dict[str, Any]) -> None:
        """
        Raw 메시지 1건 append.
        허용 필드: type/topic/tags/content/timestamp/importance/category/(옵션)level/source_span
        중요도 기본은 CAIA_IMPORTANCE_BASE 사용.
        """
        content = (data.get("content") or "").strip()
        ts = data.get("timestamp") or _now_iso()

        # 기본 중요도
        try:
            base = float(data.get("importance", CAIA_IMPORTANCE_BASE))
        except Exception:
            base = CAIA_IMPORTANCE_BASE

        # 📌 자연어 트리거/결정 패턴 감지 → 중요도/태그 강화
        try:
            if any(t and (t in content) for t in MANUAL_TRIGGERS):
                base = max(base, 0.85)
                tags = set(data.get("tags", [])); tags.add("manual-feedback"); data["tags"] = list(tags)
            elif any(p and (p in content) for p in DECISION_PATTERNS):
                base = max(base, 0.70)
                tags = set(data.get("tags", [])); tags.add("implicit-decision"); data["tags"] = list(tags)
        except Exception as e:
            print(f"[CaiaMemory] 트리거 감지 예외: {e}")

        msg = AIMessage(content=content)
        # 표준 메타 세팅
        for key, default in [
            ("type", "dialogue"),
            ("topic", "일반"),
            ("tags", []),
            ("importance", base),
            ("timestamp", ts),
            ("category", None),
            ("level", None),
            ("source_span", None),
        ]:
            setattr(msg, key, data.get(key, default))

        # 메모리 상한 안전장치(가장 오래된 것 제거)
        messages = getattr(self.chat_memory, "messages", [])
        if RAW_MAX_ITEMS > 0 and len(messages) >= RAW_MAX_ITEMS:
            try:
                for i, m in enumerate(messages):
                    if isinstance(m, AIMessage):
                        del messages[i]
                        break
            except Exception as e:
                print(f"[CaiaMemory] RAW 상한 정리 실패: {e}")

        self.chat_memory.add_message(msg)

        # 옵션: 파일 백업
        if RAW_BACKUP_PATH:
            try:
                backup_obj = {
                    "type": getattr(msg, "type", "dialogue"),
                    "topic": getattr(msg, "topic", "일반"),
                    "tags": getattr(msg, "tags", []),
                    "content": getattr(msg, "content", ""),
                    "timestamp": getattr(msg, "timestamp", ts),
                    "importance": getattr(msg, "importance", 0.0),
                    "level": getattr(msg, "level", None),
                    "source_span": getattr(msg, "source_span", None),
                    "session_id": self.session_id,
                }
                _append_jsonl(RAW_BACKUP_PATH, backup_obj)
            except Exception as e:
                print(f"[CaiaMemory] RAW 백업 예외: {e}")

    def save(self, data: Dict[str, Any]) -> None:
        self.echo(data)

    # ───────────────────── Vector Add (배치/보조용) ───────────────────── #
    def add_vector_memory(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        배치(merge/legacy/train) 또는 보조(invoke)에서 호출.
        """
        vs = getattr(self, "vectorstore", None)
        if not vs or not text:
            return
        md = dict(metadata or {})
        md.setdefault("ts", _now_iso())
        try:
            vs.add_texts([text], metadatas=[md])
        except Exception as e:
            print(f"[CaiaMemory] vector add 실패: {e}")

    # ───────────────────── Vector Search ───────────────────── #
    def vector_search(self, query: str, *, top_k: int = 5, topics_any: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Qdrant 유사도 검색. 반환: [{text, score, metadata}, ...]
        토픽 필터: qmodels.Filter 사용 (MatchAny)
        """
        vs = getattr(self, "vectorstore", None)
        if not vs or not query:
            return []
        q = query.strip()
        try:
            q_filter = None
            if topics_any:
                q_filter = qmodels.Filter(
                    should=[qmodels.FieldCondition(
                        key="topic",
                        match=qmodels.MatchAny(any=topics_any)
                    )]
                )
            docs_scores = vs.similarity_search_with_score(q, k=max(1, int(top_k)), filter=q_filter)
            out: List[Dict[str, Any]] = []
            for doc, score in docs_scores:
                out.append({
                    "text": doc.page_content,
                    "score": float(score) if score is not None else 0.0,
                    "metadata": dict(getattr(doc, "metadata", {}) or {}),
                })
            return out
        except Exception as e:
            print(f"[CaiaMemory] vector search 실패: {e}")
            return []

    # ───────────────────── Raw 조회/정리 유틸 ───────────────────── #
    def iter_raw(self) -> Iterable[Dict[str, Any]]:
        """history.messages → 표준 dict"""
        for m in getattr(self.chat_memory, "messages", []):
            if not isinstance(m, AIMessage):
                pass  # 필요 시 HumanMessage 포함 확장
            yield {
                "type": getattr(m, "type", "dialogue"),
                "topic": getattr(m, "topic", "일반"),
                "tags": getattr(m, "tags", []),
                "content": getattr(m, "content", ""),
                "timestamp": getattr(m, "timestamp", None),
                "importance": getattr(m, "importance", 0.0),
                "level": getattr(m, "level", None),
                "source_span": getattr(m, "source_span", None),
            }

    def list_raw(
        self,
        *,
        min_importance: float = 0.0,
        since_ts: Optional[str] = None,
        topic: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        배치에서 사용할 필터링된 Raw 조회.
        - min_importance 이상
        - since_ts(ISO) 이후
        - topic 일치(옵션)
        """
        from datetime import datetime as _dt

        def _to_ts(s: Optional[str]) -> float:
            if not s:
                return 0.0
            try:
                return _dt.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0

        since_unix = _to_ts(since_ts) if since_ts else 0.0
        out: List[Dict[str, Any]] = []
        for it in self.iter_raw():
            if it["importance"] < min_importance:
                continue
            if since_unix and _to_ts(it.get("timestamp")) < since_unix:
                continue
            if topic and it.get("topic") != topic:
                continue
            out.append(it)

        if limit is not None:
            out = out[-limit:]
        return out

    def prune_ttl(self, days: Optional[int] = None) -> int:
        """
        L0 TTL 정리(기본 RAW_TTL_DAYS). 반환: 삭제 수
        - 중요 요약(L1/L2/L3) 관리가 별도로 이뤄지므로 L0는 가볍게 유지
        """
        ttl_days = RAW_TTL_DAYS if days is None else max(0, days)
        if ttl_days <= 0:
            return 0

        from datetime import datetime as _dt, timedelta as _td
        cutoff = _dt.utcnow() - _td(days=ttl_days)

        kept = []
        removed = 0
        for m in getattr(self.chat_memory, "messages", []):
            ts = getattr(m, "timestamp", None)
            try:
                tsv = _dt.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
            except Exception:
                tsv = None
            if tsv and tsv < cutoff:
                removed += 1
                continue
            kept.append(m)

        try:
            self.chat_memory.messages = kept
        except Exception:
            pass
        if removed:
            print(f"[CaiaMemory] TTL 정리: {removed}건 제거(> {ttl_days}d)")
        return removed


# ─────────────────────────────────────────────
# LangServe용 기본 실행 함수(지연 임포트)
# ─────────────────────────────────────────────
def echo_fn(messages: List[HumanMessage]) -> List[AIMessage]:
    from function_router import CaiaFunctionRouter
    router = CaiaFunctionRouter(get_shared_memory())
    last = messages[-1].content if messages else ""
    router.analyze_and_route([{"type": "human", "content": last}], topic="ls-echo")
    return [AIMessage(content=f"Caia 기억 복원: {last}")]

# Runnable for LangServe
memory_runnable = RunnableLambda(echo_fn)


# ─────────────────────────────────────────────
# Shared Memory Singleton
# ─────────────────────────────────────────────
_GLOBAL_MEMORY: Optional[CaiaMemory] = None
def get_shared_memory() -> CaiaMemory:
    global _GLOBAL_MEMORY
    if _GLOBAL_MEMORY is None:
        _GLOBAL_MEMORY = CaiaMemory(session_id="caia-shared")
    return _GLOBAL_MEMORY


# 모듈 export 명시(가독성/정적 분석 보조)
__all__ = [
    "CaiaMemory",
    "get_shared_memory",
    "make_qdrant",
    "ensure_qdrant_collection",
    "QDRANT_COLLECTION",
    "EMBED_DIM",
]
