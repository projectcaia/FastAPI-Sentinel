# scheduler.py  — Caia 자동 루프 (KST 기준, ENV 임계 0.50 통일 + Router 호환판)
# - 임계치: ENV CAIA_IMPORTANCE_BASE (기본 0.50)
# - function_router 최신판과 호환: chat_completion 의존 제거, 내부 래퍼로 대체
# - finalize_session: router에 최근 Raw를 messages로 구성해 전달
# - /ready + /health 게이트 유지, 재시도 세션 유지
# - 스케줄: UMA 00:10 → Archive 00:30 → Merge 01:00 → Finalize 01:05 → Train 01:10 → Report 01:20
# - Legacy(유산화): 토요일 01:40

import os
import time
import json
import logging
import schedule
import requests
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Router (최신판)
from function_router import CaiaFunctionRouter

# 메모리 공유 싱글톤
try:
    from memory_manager import get_shared_memory
except ImportError:
    # 구버전 호환 폴백
    from memory_manager import CaiaMemory as _CaiaMemory  # type: ignore
    _GLOBAL_MEMORY = None
    def get_shared_memory():
        global _GLOBAL_MEMORY
        if _GLOBAL_MEMORY is None:
            _GLOBAL_MEMORY = _CaiaMemory(session_id="caia-shared")  # type: ignore
        return _GLOBAL_MEMORY

from memory_archive import run_archive_job
from memory_train import run_train_job

# ───────────────────── 설정 ─────────────────────
LOG_DIR = "storage/logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("scheduler")

# ENV 기본값 통일
CAIA_IMPORTANCE_BASE = float(os.getenv("CAIA_IMPORTANCE_BASE", "0.50"))
MERGE_WINDOW_HOURS   = int(os.getenv("CAIA_MERGE_WINDOW_HOURS", "24"))   # 일일 병합 창
LEGACY_WINDOW_DAYS   = int(os.getenv("CAIA_LEGACY_WINDOW_DAYS", "7"))    # 유산화 기간
OPENAI_MODEL         = os.getenv("OPENAI_MODEL", "gpt-4.1")

# 부팅 옵션
SCHED_RUN_ON_BOOT = os.getenv("SCHED_RUN_ON_BOOT", "0") == "1"
SCHED_RUN_NOW     = os.getenv("SCHED_RUN_NOW", "")  # 예: "invoke,snapshot"

KST = timezone(timedelta(hours=9))


def log_event(name: str, status: str, detail: str | None = None):
    now = datetime.now(KST).isoformat()
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{name}.log"), "a", encoding="utf-8") as f:
        f.write(f"[{status.upper()}] {now} {('- ' + detail) if detail else ''}\n")


# ────────────────── 유틸 ──────────────────
def _sanitize_env(s: str) -> str:
    if not s:
        return ""
    bad = {ord(c): None for c in "\t\r\n \u200b\u200c\u200d\ufeff"}
    return s.strip().translate(bad)


# ────────────────── URL Resolver ──────────────────
def _resolve_base() -> str:
    """
    PUBLIC_BASE_URL / FUNCTION_CALLING_URL 어느 쪽이든 받아서
    베이스 URL로 정규화 (끝 슬래시 제거)
    """
    base = _sanitize_env(os.getenv("PUBLIC_BASE_URL") or os.getenv("FUNCTION_CALLING_URL", ""))
    return base.rstrip("/")

def _resolve_invoke_url() -> str:
    """
    최종 /memory/invoke 엔드포인트로 정규화
    """
    base = _resolve_base()
    if not base:
        return ""  # 비어있으면 호출 스킵
    if base.endswith("/memory/invoke"):
        return base
    if base.endswith("/memory"):
        return f"{base}/invoke"
    return f"{base}/memory/invoke"


def _session_with_retries():
    s = requests.Session()
    r = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=r))
    s.mount("https://", HTTPAdapter(max_retries=r))
    return s


def _router():
    """공유 메모리에 결합된 라우터 반환"""
    mem = get_shared_memory()
    return CaiaFunctionRouter(mem)


def _health_gate(base_url: str) -> bool:
    """
    /ready, /health 통과 시에만 True.
    실패하면 False (네트워크/앱죽음 방지).
    """
    if not base_url:
        logger.warning("[Gate] PUBLIC_BASE_URL/FUNCTION_CALLING_URL 비어있음 → 외부 호출 스킵")
        return False

    ready_url = f"{base_url}/ready"
    health_url = f"{base_url}/health"

    try:
        with _session_with_retries() as s:
            r1 = s.get(ready_url, timeout=6)
            r2 = s.get(health_url, timeout=6)
        ok1 = r1.ok and ('"ready"' in r1.text or '"status":"ready"' in r1.text)
        ok2 = r2.ok and '"qdrant":"ok"' in r2.text
        if not ok1 or not ok2:
            logger.warning("[Gate] 헬스 불통 ready=%s health=%s text1=%s text2=%s",
                           r1.status_code, r2.status_code, (r1.text[:120] if r1.text else ""), (r2.text[:120] if r2.text else ""))
        return bool(ok1 and ok2)
    except Exception as e:
        logger.error("[Gate] 헬스 호출 예외: %s", e)
        return False


# ────────────── OpenAI 요약 래퍼 ──────────────
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
    except Exception as e:
        logger.warning("[LLM] chat_completion 예외: %s", e)
        return ""


# ────────────── ① invoke 루프 (보조) ──────────────
def run_invoke():
    """외부 엔드포인트 /memory/invoke 호출 → 요약 초안 저장(보조용)."""
    try:
        base = _resolve_base()
        target_url = _resolve_invoke_url()
        logger.info("[Invoke] base=%s target=%s", base or "(empty)", target_url or "(empty)")

        if not target_url or not _health_gate(base):
            log_event("invoke", "skip", "health-gate-fail-or-empty-base")
            logger.warning("[Invoke] 스킵됨(베이스 비었거나 헬스 불통)")
            return

        payload = {
            "messages": [
                {"type": "system", "content": f"[Caia 자동 판단 루프] {datetime.now(KST).isoformat()}"}
            ]
        }

        with _session_with_retries() as s:
            res = s.post(target_url, json=payload, timeout=20, headers={"Content-Type": "application/json"})
        if 200 <= res.status_code < 300:
            log_event("invoke", "success", f"HTTP {res.status_code}")
            logger.info("[Invoke] ✅ %s", (res.text or "")[:300])
        else:
            log_event("invoke", "fail", f"HTTP {res.status_code} {res.text[:180] if res.text else ''}")
            logger.error("[Invoke] ❌ HTTP %s %s", res.status_code, (res.text or "")[:300])

    except Exception as e:
        log_event("invoke", "fail", traceback.format_exc())
        logger.error("[Invoke] 예외 ❌ %s", e)


# ────────────── ② 스냅샷/아카이브/트레인/리트리브 ──────────────
def run_snapshot():
    try:
        r = _router()
        # UMA 입력 스냅샷: 시스템 로그를 Raw에 남김 (요약은 생략)
        r.analyze_and_route(
            messages=[{"type": "system", "content": f"✅ UMA snapshot {datetime.now(KST).isoformat()}"}],
            topic="UMA",
            do_micro_digest=False
        )
        r.memory.save({"type": "system", "content": "✅ snapshot", "category": "스케줄러"})
        log_event("snapshot", "success")
        logger.info("[Snapshot] 완료 ✅")
    except Exception as e:
        log_event("snapshot", "fail", traceback.format_exc())
        logger.error("[Snapshot] 예외 ❌ %s", e)


def run_archive():
    try:
        res = run_archive_job()
        log_event("archive", "success", f"archived={res.get('archived')} path={res.get('path')}")
        logger.info("[Archive] 완료 ✅ archived=%s file=%s", res.get("archived"), res.get("path"))
    except Exception as e:
        log_event("archive", "fail", traceback.format_exc())
        logger.error("[Archive] 예외 ❌ %s", e)


def run_train():
    try:
        res = run_train_job()
        log_event("train", "success", f"trained={res.get('trained')}")
        logger.info("[Train] 완료 ✅ trained=%s topics=%s", res.get("trained"), res.get("topics"))
    except Exception as e:
        log_event("train", "fail", traceback.format_exc())
        logger.error("[Train] 예외 ❌ %s", e)


def run_retrieve_digest():
    try:
        r = _router()
        keywords = ["콜매도", "Reflex", "알파 전략 실패"]
        digest = [{"q": kw, "results": r.retrieve(kw)} for kw in keywords]
        os.makedirs("storage", exist_ok=True)
        path = os.path.join("storage", f"digest_{datetime.utcnow().isoformat()}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(digest, f, ensure_ascii=False, indent=2)
        r.memory.save({"type": "system", "content": "✅ retrieve_digest", "category": "스케줄러"})
        log_event("retrieve_digest", "success")
        logger.info("[RetrieveDigest] 저장 완료 ✅ %s", path)
    except Exception as e:
        log_event("retrieve_digest", "fail", traceback.format_exc())
        logger.error("[RetrieveDigest] 예외 ❌ %s", e)


# ────────────── ③ 병합 요약 (핵심, L1 우선) ──────────────
def _priority_score(item: dict) -> float:
    """L1(수동/결정/초안) 우선 가중치 + importance"""
    base = float(item.get("importance", 0.0))
    t = (item.get("type") or "").lower()
    tags = set(item.get("tags") or [])
    bonus = 0.0
    if "manual-feedback" in tags or t == "manual":
        bonus += 0.25
    if "implicit-decision" in tags or t == "decision":
        bonus += 0.20
    if t == "digest_draft":
        bonus += 0.10
    return min(1.0, base + bonus)

def run_merge_digest():
    """
    지난 MERGE_WINDOW_HOURS Raw 중 중요도 임계 이상 + L1 우선 항목을 topic별로 묶어
    700~900자 Digest 생성 → Qdrant에 level='long'으로 저장.
    """
    try:
        r = _router()
        memory = r.memory

        if not getattr(memory, "vectorstore", None):
            log_event("merge_digest", "success", "vectorstore-missing")
            logger.warning("[MergeDigest] vectorstore 미탑재 → 임베딩/저장 스킵")
            return

        since_ts = (datetime.utcnow() - timedelta(hours=MERGE_WINDOW_HOURS)).isoformat()
        items = memory.list_raw(min_importance=CAIA_IMPORTANCE_BASE, since_ts=since_ts)

        if not items:
            log_event("merge_digest", "success", "no items")
            logger.info("[MergeDigest] 중요도 기준 충족 항목 없음")
            return

        # 토픽 그룹 + 우선순위 정렬(L1 가중치)
        groups: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            groups[it.get("topic", "일반")].append(it)

        created = 0
        for topic, arr in groups.items():
            # 우선순위 점수로 정렬 후 상위 N개만 사용 (과다 길이 방지)
            arr_sorted = sorted(arr, key=_priority_score, reverse=True)
            top = arr_sorted[:20]  # topic당 최대 20 bullet

            bullets = "\n".join(f"- {x.get('content','')[:500]}" for x in top)
            prompt = f"""다음 핵심을 중복 없이 700~900자 한국어로 요약해.
목적: Caia 장기 기억(Digest). 우선순위: 수동피드백/결정/초안 > 일반.
형식: 결론→근거→다음 행동. 과장 금지.
---
{bullets}
"""
            digest = _chat_completion(prompt, max_tokens=700, temperature=0.2) or ""
            if not digest.strip():
                continue

            source_span = (top[0].get("timestamp"), top[-1].get("timestamp"))
            memory.add_vector_memory(
                digest,
                metadata={
                    "level": "long",
                    "topic": topic,
                    "tags": ["merge", "l1-priority"],
                    "source_span": list(source_span),
                    "ts": datetime.utcnow().isoformat(),
                },
            )
            created += 1

        log_event("merge_digest", "success", f"created={created}")
        logger.info("[MergeDigest] 생성 건수 ✅ %d", created)
    except Exception as e:
        log_event("merge_digest", "fail", traceback.format_exc())
        logger.error("[MergeDigest] 예외 ❌ %s", e)


# ────────────── ④ 유산화 (주 1회) ──────────────
def run_legacy_promote():
    """
    최근 7일 Raw(중요도 기준 충족)를 OBAR(원인-행동-결과-교훈)로 1~2p 서술.
    Qdrant에 level='legacy'로 저장.
    """
    try:
        r = _router()
        memory = r.memory

        if not getattr(memory, "vectorstore", None):
            log_event("legacy_promote", "success", "vectorstore-missing")
            logger.warning("[Legacy] vectorstore 미탑재 → 임베딩/저장 스킵")
            return

        since_ts = (datetime.utcnow() - timedelta(days=LEGACY_WINDOW_DAYS)).isoformat()
        items = memory.list_raw(min_importance=CAIA_IMPORTANCE_BASE, since_ts=since_ts)

        if not items:
            log_event("legacy_promote", "success", "no items")
            logger.info("[Legacy] 최근 7일 중요 항목 없음")
            return

        groups = defaultdict(list)
        for it in items:
            groups[it.get("topic", "일반")].append(it)

        created = 0
        for topic, arr in groups.items():
            # OBAR 서술
            body = "\n".join(f"- {x.get('content','')[:500]}" for x in arr)
            prompt = f"""지난 7일간 '{topic}' 관련 항목을 OBAR로 1~2페이지 분량 한국어 서술로 정리해.
형식:
[원인] ...
[행동] ...
[결과] ...
[교훈] ...
군더더기 없이 실무 문서 톤으로.
---
{body}
"""
            legacy_text = _chat_completion(prompt, max_tokens=1200, temperature=0.2) or ""
            if not legacy_text.strip():
                continue

            iso = datetime.utcnow().isocalendar()
            week_id = f"{iso[0]}-W{iso[1]:02d}"

            memory.add_vector_memory(
                legacy_text,
                metadata={
                    "level": "legacy",
                    "topic": topic,
                    "week_id": week_id,
                    "ts": datetime.utcnow().isoformat(),
                },
            )
            created += 1

        log_event("legacy_promote", "success", f"created={created}")
        logger.info("[Legacy] 승격 건수 ✅ %d", created)
    except Exception as e:
        log_event("legacy_promote", "fail", traceback.format_exc())
        logger.error("[Legacy] 예외 ❌ %s", e)


# ────────────── ⑤ 세션 마감(L2) 자동화 ──────────────
def run_finalize_session():
    """
    최근 MERGE_WINDOW_HOURS 창을 L2(Session-Digest)로 승격.
    function_router 최신판의 finalize_session(messages, topic) 서명과 호환되도록,
    최근 Raw를 messages 리스트로 구성해 전달한다.
    """
    try:
        r = _router()
        memory = r.memory
        since_ts = (datetime.utcnow() - timedelta(hours=MERGE_WINDOW_HOURS)).isoformat()
        items = memory.list_raw(min_importance=CAIA_IMPORTANCE_BASE, since_ts=since_ts)

        if not items:
            log_event("finalize_session", "success", "no items")
            logger.info("[FinalizeSession] 최근 창 내 항목 없음")
            return

        # 시스템 메시지 제외하고 사람/AI 발화만 messages로 구성
        msgs = []
        for it in items:
            t = (it.get("type") or "human").lower()
            if t == "system":
                continue
            c = (it.get("content") or "").strip()
            if not c:
                continue
            msgs.append({"type": t, "content": c})

        res = r.finalize_session(messages=msgs, topic=None)
        created = res.get("created", 0) if isinstance(res, dict) else 0
        log_event("finalize_session", "success", f"created={created}")
        logger.info("[FinalizeSession] L2 생성 건수 ✅ %s", created)
    except Exception as e:
        log_event("finalize_session", "fail", traceback.format_exc())
        logger.error("[FinalizeSession] 예외 ❌ %s", e)


# ────────────────── ⑥ 스케줄러 부트스트랩 ──────────────────
def start_scheduler():
    logger.info("🟢 [Caia Scheduler] 스케줄러 루프 시작됨 (TZ=%s)", os.getenv("TZ", "system-default"))

    # 중복 방지
    schedule.clear('caia')

    # 문서 기준(KST): UMA 00:10 → Archive 00:30 → **Merge 01:00** → Finalize 01:05 → Train 01:10 → Report 01:20
    schedule.every().day.at("00:10").do(run_snapshot).tag('caia')           # UMA 입력 스냅샷
    schedule.every().day.at("00:30").do(run_archive).tag('caia')            # 아카이브
    schedule.every().day.at("01:00").do(run_merge_digest).tag('caia')       # ✅ 핵심: L1우선 Digest 생성/임베딩
    schedule.every().day.at("01:05").do(run_finalize_session).tag('caia')   # ✅ L2 세션 마감 요약
    schedule.every().day.at("01:10").do(run_train).tag('caia')              # 보강(요약 임베딩)
    schedule.every().day.at("01:20").do(run_retrieve_digest).tag('caia')    # 리포트(선택)

    # 유산화: **토요일 새벽만 01:40** 고정 (주1회)
    schedule.every().saturday.at("01:40").do(run_legacy_promote).tag('caia')

    # 다음 실행 시각 로그
    for job in schedule.get_jobs('caia'):
        logger.info("⏰ 스케줄 등록: %-18s → next_run=%s", job.job_func.__name__, job.next_run)

    # 부팅점검: 즉시 한 번 실행(옵션)
    if SCHED_RUN_ON_BOOT:
        logger.info("🚀 부팅후 즉시 점검(SCHED_RUN_ON_BOOT=1)")
        try:
            run_invoke()
            run_snapshot()
        except Exception as e:
            logger.error("부팅점검 예외: %s", e)

    if SCHED_RUN_NOW:
        # 예: SCHED_RUN_NOW="invoke,snapshot,merge_digest"
        now_jobs = [j.strip() for j in SCHED_RUN_NOW.split(",") if j.strip()]
        logger.info("⚡ 수동 즉시 실행(SCHED_RUN_NOW): %s", now_jobs)
        for name in now_jobs:
            try:
                globals()[f"run_{name}"]()
            except KeyError:
                logger.warning("알 수 없는 즉시 실행 잡: %s", name)
            except Exception as e:
                logger.error("즉시 실행 예외(%s): %s", name, e)

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    start_scheduler()
