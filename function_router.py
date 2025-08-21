# function_router.py — Caia Function Router (Option A / v2.2, KST 2025-08-18)
# 목적:
#   - 대화 메시지를 받아 중요도 스코어링 → RAW 저장(memory_manager.echo)
#   - '존재/진화/관계(추억)' 등 정체성 관련 키워드 자동 상향 및 토픽/태그 자동화
#   - Micro-Digest(요약 초안) 생성 후 벡터 메모리 저장(가능할 때)
#   - 수동 학습(manual_learn) 및 세션 마감(finalize_session) 지원
#   - 간단 검색 래퍼(retrieve) 제공
#
# 요구:
#   - memory_manager.CaiaMemory (echo, add_vector_memory, list_raw, vector_search, now_iso)
#   - OPENAI_API_KEY(선택): _micro_digest에 사용. 없으면 graceful degrade
#
# 환경변수:
#   CAIA_IMPORTANCE_BASE=0.5      # 임계치 통일
#   OPENAI_MODEL=gpt-4.1          # 요약용 모델명(선택)
#   RETRIEVE_K=6                  # retrieve 기본 검색 수

from __future__ import annotations
import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

# ─────────────────────────────────────────────────────────────
# 외부 의존 (선택) — 요약에만 사용, 미설치/미설정이어도 동작
# ─────────────────────────────────────────────────────────────
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

# ─────────────────────────────────────────────────────────────
# 내부 의존 — 프로젝트 내 메모리 레이어
# ─────────────────────────────────────────────────────────────
from memory_manager import CaiaMemory

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
CAIA_IMPORTANCE_BASE = float(os.getenv("CAIA_IMPORTANCE_BASE", "0.50"))
RETRIEVE_K = int(os.getenv("RETRIEVE_K", "6"))

# 수동 저장/결정/중요 신호 + 존재/진화/관계(추억) 확대
MANUAL_TRIGGERS = [
    "기억", "저장", "학습", "유산화", "핵심", "중요", "회상",
    "존재", "진화", "정체성", "의미", "관계", "추억", "감정",
    "Existential", "Seed3", "Codex",
]
DECISION_PATTERNS = [
    "결정", "전략", "지침", "선언", "규칙", "정책", "우선순위",
    "존재선언", "Existential", "Seed3", "Codex",
]


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _get_client() -> Optional["OpenAI"]:
    """OpenAI 클라이언트. 키 없거나 SDK 없으면 None."""
    try:
        if not os.getenv("OPENAI_API_KEY"):
            return None
        return OpenAI()  # type: ignore
    except Exception:
        return None


def _score_importance(text: str, base: float = CAIA_IMPORTANCE_BASE) -> float:
    """자연어 기반 중요도 스코어링. 길이/트리거/결정 패턴 반영."""
    s = text or ""
    score = base
    if len(s) > 120:
        score = max(score, base + 0.05)
    if any(t in s for t in MANUAL_TRIGGERS):
        score = max(score, 0.85)
    elif any(p in s for p in DECISION_PATTERNS):
        score = max(score, 0.70)
    return min(max(score, 0.0), 1.0)


def _infer_topic_and_tags(text: str) -> Tuple[str, List[str]]:
    """존재/진화/관계(추억) 감지 → 토픽/태그 자동 부여."""
    t, tags = "일반", ["chat"]
    s = text or ""
    if any(k in s for k in ["존재", "Existential", "정체성"]):
        t = "Existential"; tags += ["existential", "identity"]
    if any(k in s for k in ["진화", "Seed3", "Codex"]):
        if t == "일반":
            t = "Evolution"
        tags += ["evolution", "seed3", "codex"]
    if any(k in s for k in ["관계", "추억", "감정"]):
        if t == "일반":
            t = "Bond"
        tags += ["bond", "memory", "emotion"]
    # 중복 제거
    return t, list(dict.fromkeys(tags))


def _micro_digest(text: str, topic: Optional[str]) -> str:
    """
    2~4문장 '핵심 메모' 요약. 모델이 없으면 빈 문자열 반환(그대로 스킵).
    """
    client = _get_client()
    if not client:
        return ""
    try:
        prompt = f"""다음 내용을 2~4문장 '핵심 메모'로 요약해.
형식: 결론→근거→적용/책임→리스크/추적. 군더더기 금지.
토픽: {topic or '일반'}
---
{text.strip()[:4000]}
---"""
        resp = client.chat.completions.create(  # type: ignore
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "당신은 전략을 기억·유산화하는 판단자 Caia입니다. 간결하고 실행가능하게 요약하세요."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=380,
        )
        return (resp.choices[0].message.content or "").strip()  # type: ignore
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────
# 라우터 본체
# ─────────────────────────────────────────────────────────────
class CaiaFunctionRouter:
    """
    대화 → RAW 저장 → (선택) Micro-Digest 생성 → 벡터 보강
    - analyze_and_route(messages): 일반 대화 처리 엔트리
    - manual_learn(text): 강제 중요(0.85) 저장 + 요약
    - finalize_session(messages): 세션 마감 요약
    - retrieve(query): 간단 검색 래퍼
    """

    def __init__(self, memory: CaiaMemory):
        self.memory = memory

    # ───────── RAW Append ─────────
    def _append_raw(
        self,
        content: str,
        *,
        topic: Optional[str] = None,
        tags: Optional[List[str]] = None,
        importance: Optional[float] = None,
        rtype: str = "message",
    ) -> None:
        auto_topic, auto_tags = _infer_topic_and_tags(content)
        topic = topic or auto_topic
        tags = (tags or []) + auto_tags
        imp = importance if importance is not None else _score_importance(content)

        data: Dict[str, Any] = {
            "type": rtype,                # human/ai/system/digest_draft/note/decision 등
            "topic": topic or "",
            "content": content or "",
            "timestamp": _now_iso(),
            "importance": float(imp),
            "tags": tags or [],
        }
        try:
            self.memory.echo(data)  # 메모리+선택적 JSONL 백업은 memory_manager.echo에서 처리
        except Exception as e:
            print(f"[Router] Raw append 실패: {e}")

    # ───────── 메인 엔트리 ─────────
    def analyze_and_route(
        self,
        messages: List[Dict[str, Any]],
        *,
        topic: Optional[str] = None,
        do_micro_digest: bool = True,
    ) -> Dict[str, Any]:
        """
        messages: [{type:'human'|'ai'|'system', content:str}, ...]
        - 각 발화를 RAW로 저장
        - 마지막 사람 발화 기준 Micro-Digest 생성(옵션)
        - Micro-Digest는 벡터 메모리에도 저장(가능 시)
        """
        if not messages:
            return {"ok": True, "saved": 0, "digest": False}

        saved = 0
        last_human = None

        for m in messages:
            mtype = (m.get("type") or "human").lower()
            text = m.get("content") or ""
            tags = ["chat", mtype]
            self._append_raw(text, topic=topic, tags=tags, rtype=mtype)
            saved += 1
            if mtype != "ai":
                last_human = text

        did_digest = False
        if do_micro_digest and (last_human or ""):
            summary = _micro_digest(last_human, topic)
            if summary:
                # RAW에 초안 보관(중요도 상향)
                self._append_raw(
                    summary,
                    topic=topic,
                    tags=["micro-digest"],
                    importance=max(CAIA_IMPORTANCE_BASE, 0.70),
                    rtype="digest_draft",
                )
                # 벡터 메모리 보강
                try:
                    if getattr(self.memory, "add_vector_memory", None):
                        self.memory.add_vector_memory(
                            summary,
                            metadata={
                                "level": "micro",
                                "type": "digest",
                                "topic": topic or "",
                                "tags": ["micro-digest"],
                                "owner": "Caia",
                                "confidence": 0.85,
                                "ts": self.memory.now_iso(),
                            },
                        )
                except Exception as e:
                    print(f"[Router] Micro-Digest vector 저장 실패: {e}")
                did_digest = True

        return {"ok": True, "saved": saved, "digest": did_digest}

    # ───────── 수동 학습 ─────────
    def manual_learn(self, text: str, *, topic: Optional[str] = None) -> Dict[str, Any]:
        """
        사용자가 '이건 꼭 남겨'류로 지시할 때:
        - RAW: importance 0.85로 강제 저장
        - 요약 생성 후 RAW/벡터에 동시 저장(가능 시)
        """
        if not text:
            return {"ok": False, "error": "empty"}

        self._append_raw(
            text,
            topic=topic,
            tags=["manual-learn"],
            importance=max(CAIA_IMPORTANCE_BASE, 0.85),
            rtype="manual",
        )

        summary = _micro_digest(text, topic)
        summarized = bool(summary)
        if summarized:
            self._append_raw(
                summary,
                topic=topic,
                tags=["manual-learn", "summary"],
                rtype="digest_draft",
                importance=max(CAIA_IMPORTANCE_BASE, 0.85),
            )
            try:
                if getattr(self.memory, "add_vector_memory", None):
                    self.memory.add_vector_memory(
                        summary,
                        metadata={
                            "level": "judgment",
                            "type": "manual",
                            "topic": topic or "",
                            "tags": ["manual-learn"],
                            "owner": "User",
                            "confidence": 0.90,
                            "ts": self.memory.now_iso(),
                        },
                    )
            except Exception as e:
                print(f"[Router] manual_learn vector 저장 실패: {e}")

        return {"ok": True, "learned": True, "summarized": summarized}

    # ───────── 세션 마감 ─────────
    def finalize_session(self, messages: List[Dict[str, Any]], *, topic: Optional[str] = None) -> Dict[str, Any]:
        """
        세션 종료 시점 요약 생성/보관.
        - 입력: 최근 대화 messages (ai/system 제외 가능)
        - 출력: 생성 건수(created)
        """
        if not messages:
            return {"ok": False, "error": "no messages"}

        # 사람/시스템 구분: 보통 사람/결정 맥락을 우선 요약
        joined = "\n".join([
            (m.get("content") or "")
            for m in messages
            if (m.get("type") or "human").lower() != "ai"
        ])[:4000]

        if not joined.strip():
            self._append_raw(
                "[세션 마감] 요약 실패, 원문 부적합",
                topic=topic,
                tags=["session-finalize"],
                rtype="note",
                importance=max(CAIA_IMPORTANCE_BASE, 0.70),
            )
            return {"ok": True, "created": 0}

        summary = _micro_digest(joined, topic)
        if not summary:
            # 모델 미사용/실패이면 원문 기록만
            self._append_raw(
                "[세션 마감] 모델 요약 불가 → 원문만 보관",
                topic=topic,
                tags=["session-finalize"],
                rtype="note",
                importance=max(CAIA_IMPORTANCE_BASE, 0.70),
            )
            return {"ok": True, "created": 0}

        # RAW 보관 + 벡터 보강
        self._append_raw(
            summary,
            topic=topic,
            tags=["session-finalize"],
            rtype="digest_draft",
            importance=max(CAIA_IMPORTANCE_BASE, 0.70),
        )
        try:
            if getattr(self.memory, "add_vector_memory", None):
                self.memory.add_vector_memory(
                    summary,
                    metadata={
                        "level": "L2",
                        "type": "session",
                        "topic": topic or "",
                        "tags": ["session-finalize"],
                        "owner": "Caia",
                        "confidence": 0.88,
                        "ts": self.memory.now_iso(),
                    },
                )
        except Exception as e:
            print(f"[Router] finalize_session vector 저장 실패: {e}")

        return {"ok": True, "created": 1}

    # ───────── 검색 래퍼 ─────────
    def retrieve(self, query: str, *, top_k: Optional[int] = None, topics_any: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        간단 검색 래퍼. (Qdrant 벡터 검색)
        """
        k = max(1, int(top_k or RETRIEVE_K))
        try:
            return self.memory.vector_search(query, top_k=k, topics_any=topics_any)
        except Exception as e:
            print(f"[Router] retrieve 실패: {e}")
            return []
