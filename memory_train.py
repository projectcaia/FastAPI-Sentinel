# memory_train.py — 아카이브 → 요약 보강 임베딩 (v2.1 / ENV 임계 0.50 통일, 독립형 요약 래퍼)
# 목적: 최근 아카이브 파일을 읽어 주제별 핵심 요약(700~900자)을 생성하고
#       Qdrant에 level='long' + tags=['summary','train'] 형태로 저장
# 변경점:
#   - CAIA_IMPORTANCE_BASE 기본값 0.50로 통일 (ENV 우선)
#   - function_router.chat_completion 의존 제거 → 내부 _chat_completion()로 독립
#   - 내구성 강화(에러 내성, 손상 파일 무시), 중복 방지 digest_hash 유지

from __future__ import annotations

import os
import json
import glob
import hashlib
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

from memory_manager import CaiaMemory

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
ARCHIVE_DIR            = os.getenv("CAIA_ARCHIVE_DIR", "storage/archive")
TRAIN_MAX_FILES        = int(os.getenv("CAIA_TRAIN_MAX_FILES", "3"))         # 최근 N개 아카이브만 사용
CAIA_IMPORTANCE_BASE   = float(os.getenv("CAIA_IMPORTANCE_BASE", "0.50"))    # ★ 임계치 기본 0.50
SUMMARY_MAX_TOKENS     = int(os.getenv("CAIA_TRAIN_MAX_TOKENS", "700"))      # 대략 700 tokens ≈ 700~900자
TOPIC_MIN_ITEMS        = int(os.getenv("CAIA_TRAIN_TOPIC_MIN_ITEMS", "2"))   # 토픽별 최소 항목 수(미만이면 스킵)
BULLET_LIMIT_PER_TOPIC = int(os.getenv("CAIA_TRAIN_BULLET_LIMIT", "30"))     # 토픽별 bullet 최대 수
OPENAI_MODEL           = os.getenv("OPENAI_MODEL", "gpt-4.1")


# ─────────────────────────────────────────────
# OpenAI 요약 래퍼 (독립형)
# ─────────────────────────────────────────────
def _openai_client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key)
    except Exception:
        return None

def _chat_completion(prompt: str, *, max_tokens: int = 700, temperature: float = 0.2) -> str:
    client = _openai_client()
    if not client:
        return ""
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "당신은 전략을 기억·유산화하는 판단자 Caia입니다. 간결하고 실행가능하게 요약하세요."},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def _list_recent_archives(n: int = TRAIN_MAX_FILES) -> List[str]:
    files = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "archive_*.json")))
    return files[-n:] if n > 0 else files

def _load_archive(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

def _pick_items(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    # 중요도(desc) → timestamp(desc) 정렬 후 상위 limit
    def _key(it):
        imp = float(it.get("importance", 0.0) or 0.0)
        ts  = it.get("timestamp") or ""
        return (imp, ts)
    return sorted(items, key=_key, reverse=True)[:limit]


# ─────────────────────────────────────────────
# 핵심 요약 생성
# ─────────────────────────────────────────────
def _summarize_topic(topic: str, bullets: List[str]) -> str:
    joined = "\n".join(f"- {b}" for b in bullets)
    prompt = f"""아래 항목들을 중복 없이 700~900자 한국어로 요약해.
목적: Caia 장기 기억 보강(Digest). 형식: 결론→근거→다음 행동. 과장은 금지.
토픽: {topic}
---
{joined}
"""
    return _chat_completion(prompt, max_tokens=SUMMARY_MAX_TOKENS, temperature=0.2) or ""


# ─────────────────────────────────────────────
# 메인 작업: 아카이브 → 요약 → 임베딩
# ─────────────────────────────────────────────
def run_train_job() -> Dict[str, Any]:
    mem = CaiaMemory(session_id="caia-session")
    vs = getattr(mem, "vectorstore", None)

    # 벡터스토어 없으면 스킵(로그만 리턴)
    if not vs:
        return {"ok": False, "reason": "vectorstore_missing"}

    paths = _list_recent_archives(TRAIN_MAX_FILES)
    if not paths:
        return {"ok": True, "trained": 0, "note": "no archive files"}

    # 아카이브 로드 → 중요도 필터 → 토픽 그룹
    topics: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    loaded_files: List[str] = []

    for p in paths:
        try:
            data = _load_archive(p)
            items = data.get("items", [])
            for it in items:
                if float(it.get("importance", 0.0) or 0.0) < CAIA_IMPORTANCE_BASE:
                    continue
                topic = it.get("topic") or "일반"
                topics[topic].append(it)
            loaded_files.append(os.path.basename(p))
        except Exception:
            # 손상된 파일은 무시
            continue

    created = 0
    details: List[Dict[str, Any]] = []

    for topic, arr in topics.items():
        if len(arr) < TOPIC_MIN_ITEMS:
            continue

        # 가장 유의미한 bullet만 추림
        picked = _pick_items(arr, BULLET_LIMIT_PER_TOPIC)
        bullets = [x.get("content", "")[:500] for x in picked if x.get("content")]
        if not bullets:
            continue

        summary = _summarize_topic(topic, bullets)
        if not summary.strip():
            continue

        # 중복 방지용 해시(메타에 기록)
        digest_hash = _hash(summary)[:12]

        # source span
        first_ts = picked[0].get("timestamp")
        last_ts  = picked[-1].get("timestamp")

        mem.add_vector_memory(
            summary,
            metadata={
                "level": "long",
                "topic": topic,
                "tags": ["summary", "train"],
                "digest_hash": digest_hash,
                "source_span": [first_ts, last_ts],
                "ts": _now_iso(),
            },
        )
        details.append({"topic": topic, "hash": digest_hash, "count": len(picked)})
        created += 1

    return {
        "ok": True,
        "trained": created,
        "files": loaded_files,
        "topics": list(topics.keys()),
        "details": details,
    }


# ──────────────── CLI 실행 지원 ────────────────
if __name__ == "__main__":
    res = run_train_job()
    print(json.dumps(res, ensure_ascii=False, indent=2))
