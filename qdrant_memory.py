# qdrant_memory.py
# Qdrant 메모리 어댑터
from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchAny,
)

# --------------------------------
# ENV & 공용
# --------------------------------
def _sanitize(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(ch, "")
    return text

QDRANT_URL = _sanitize(os.getenv("QDRANT_URL", ""))
QDRANT_API_KEY = _sanitize(os.getenv("QDRANT_API_KEY", ""))
QDRANT_COLLECTION = _sanitize(os.getenv("QDRANT_COLLECTION", "caia-memory")) or "caia-memory"
EMBED_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

# 프로세스 생애주기 동안 컬렉션 보장은 1회만 수행
_COLLECTION_ENSURED: set[str] = set()

def _make_client() -> QdrantClient:
    kwargs: Dict[str, Any] = {}
    if QDRANT_API_KEY:
        kwargs["api_key"] = QDRANT_API_KEY
    if QDRANT_URL:
        kwargs["url"] = QDRANT_URL
    return QdrantClient(**kwargs)

# --------------------------------
# Qdrant 어댑터
# --------------------------------
class QdrantMemory:
    """
    - 존재 확인 후에만 create_collection 호출 (PUT 409 노이즈 제거)
    - 컬렉션 보장은 프로세스당 1회 (중복 ensure 방지)
    - URL 공백/제로폭 방어
    """

    def __init__(self, collection: Optional[str] = None, embed_dim: Optional[int] = None):
        self.collection = collection or QDRANT_COLLECTION
        self.embed_dim = int(embed_dim or EMBED_DIM)
        self.client = _make_client()
        self._ensure_collection_once(self.collection)

    # -------- 내부: 보장/인덱스 --------
    def _ensure_collection_once(self, name: str) -> None:
        if name in _COLLECTION_ENSURED:
            return
        try:
            self.client.get_collection(name)
        except Exception:
            # 없을 때만 생성
            self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=self.embed_dim, distance=Distance.COSINE),
            )
        # 인덱스(페이로드 필드) 보장 (있으면 실패 무시)
        for field, schema in (("topic", "keyword"), ("level", "keyword"), ("ts_unix", "integer")):
            try:
                self.client.create_payload_index(name, field_name=field, field_schema=schema)
            except Exception:
                pass
        _COLLECTION_ENSURED.add(name)

    # -------- upsert/search/delete --------
    def upsert(
        self,
        points: Iterable[Dict[str, Any]],
        vectors: Iterable[List[float]],
        ids: Optional[Iterable[Any]] = None,
    ) -> Dict[str, Any]:
        """points: [{"text":..., "topic":[...], "level":"L2", "ts_unix":...}, ...]"""
        ts = int(time.time())
        payloads: List[Dict[str, Any]] = []
        q_ids: List[Any] = []
        vecs: List[List[float]] = []

        for idx, p in enumerate(points):
            payload = dict(p)
            payload.setdefault("ts_unix", ts)
            payloads.append(payload)
            vecs.append(list(next(vectors)))
            q_ids.append((list(ids)[idx] if ids is not None else None) or f"{ts}-{idx}")

        q_points = [
            PointStruct(id=q_ids[i], vector=vecs[i], payload=payloads[i]) for i in range(len(payloads))
        ]
        res = self.client.upsert(collection_name=self.collection, points=q_points, wait=True)
        return {"status": "ok", "upserted": len(q_points), "result": getattr(res, "status", "accepted")}

    def search(
        self,
        query_vector: List[float],
        top_k: int = 5,
        topics_any: Optional[List[str]] = None,
        level_any: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        flt: Optional[Filter] = None
        must: List[FieldCondition] = []

        if topics_any:
            must.append(FieldCondition(key="topic", match=MatchAny(any=topics_any)))
        if level_any:
            must.append(FieldCondition(key="level", match=MatchAny(any=level_any)))
        if must:
            flt = Filter(must=must)

        hits = self.client.search(
            collection_name=self.collection,
            query_vector=query_vector,
            limit=int(top_k),
            query_filter=flt,
        )
        out: List[Dict[str, Any]] = []
        for h in hits:
            out.append({
                "id": h.id,
                "score": float(h.score),
                "payload": h.payload or {},
            })
        return out

    def delete_ids(self, ids: List[Any]) -> Dict[str, Any]:
        res = self.client.delete(collection_name=self.collection, points_selector=ids, wait=True)
        return {"status": "ok", "deleted": len(ids), "result": getattr(res, "status", "accepted")}

    # -------- 유틸 --------
    def stats(self) -> Dict[str, Any]:
        c = self.client.get_collection(self.collection)
        return {
            "collection": self.collection,
            "vectors_count": getattr(c, "vectors_count", None),
            "config": {
                "size": self.embed_dim,
                "distance": "cosine",
            },
            "url": QDRANT_URL,
        }
