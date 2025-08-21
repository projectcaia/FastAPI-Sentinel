# memory_archive.py — Raw → 일일 아카이브 파일 저장 (v2.1, ENV 0.50 + JSONL 폴백)
# 목적: 지난 window_hours 동안 Raw 메모리를 중요도 기준으로 추려
#       storage/archive/YYYY-MM-DD_HHMMSS.json 로 저장
# 변경점:
#   - CAIA_IMPORTANCE_BASE 기본값 0.50로 통일 (ENV 우선)
#   - RAM Raw가 비었을 때 JSONL 백업(OPTIONAL: RAW_BACKUP_PATH)에서 폴백 수집
#   - 안전성 강화: 파일/레코드 검증, 중복 제거

import os
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Iterable, Set, Tuple

from memory_manager import CaiaMemory  # 동일 프로세스 RAM Raw 접근

# ──────────────────────────────────────────────────────────────
# 환경값
# ──────────────────────────────────────────────────────────────
CAIA_IMPORTANCE_BASE = float(os.getenv("CAIA_IMPORTANCE_BASE", "0.50"))
MERGE_WINDOW_HOURS   = int(os.getenv("CAIA_MERGE_WINDOW_HOURS", "24"))
ARCHIVE_DIR          = os.getenv("CAIA_ARCHIVE_DIR", "storage/archive")

# 선택: CaiaMemory.echo(...)가 Raw JSONL 백업을 남기도록 한 경로
RAW_BACKUP_PATH      = os.getenv("RAW_BACKUP_PATH", "").strip()

# ──────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _since_iso(hours: int) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()

def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None

def _norm_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    JSONL 단일 레코드 정규화: 필수 키 존재/형식 보정
    기대 키: type, topic, content, timestamp, importance, tags
    """
    if not isinstance(rec, dict):
        return None
    content = (rec.get("content") or "").strip()
    if not content:
        return None
    ts = rec.get("timestamp") or ""
    if not _parse_iso(ts):
        # 타임스탬프 없으면 스킵 (아카이브 기준에 맞추기 위해)
        return None
    try:
        imp = float(rec.get("importance", 0.0))
    except Exception:
        imp = 0.0
    return {
        "type": (rec.get("type") or "message"),
        "topic": rec.get("topic") or "",
        "content": content,
        "timestamp": ts,
        "importance": float(max(min(imp, 1.0), 0.0)),
        "tags": list(rec.get("tags") or []),
    }

def _unique_key(rec: Dict[str, Any]) -> Tuple[str, str]:
    # 중복 제거용 간단 키: (timestamp(분해도 downscale), content 해시 대용으로 앞 64자)
    ts = (rec.get("timestamp") or "")[:16]  # YYYY-MM-DDTHH:MM
    head = (rec.get("content") or "")[:64]
    return (ts, head)

# ──────────────────────────────────────────────────────────────
# 수집 경로 1: RAM Raw (동일 프로세스)
# ──────────────────────────────────────────────────────────────
def _collect_from_ram(
    *, min_importance: float, since_ts: str, topic: Optional[str], limit: Optional[int]
) -> List[Dict[str, Any]]:
    mem = CaiaMemory(session_id="caia-session")
    return mem.list_raw(
        min_importance=min_importance,
        since_ts=since_ts,
        topic=topic,
        limit=limit,
    )

# ──────────────────────────────────────────────────────────────
# 수집 경로 2: JSONL 백업 폴백 (프로세스 분리/재시작 대비)
# ──────────────────────────────────────────────────────────────
def _collect_from_jsonl_backup(
    *, path: str, min_importance: float, since_ts: str, topic: Optional[str], limit: Optional[int]
) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    try:
        since_dt = _parse_iso(since_ts) or datetime.utcnow() - timedelta(hours=MERGE_WINDOW_HOURS)
        picked: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str]] = set()

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rec = _norm_record(rec)
                if not rec:
                    continue

                # 필터: 시간
                rdt = _parse_iso(rec["timestamp"])
                if not rdt or rdt < since_dt:
                    continue

                # 필터: 중요도
                if rec["importance"] < min_importance:
                    continue

                # 필터: 토픽
                if topic and (rec.get("topic") or "") != topic:
                    continue

                key = _unique_key(rec)
                if key in seen:
                    continue
                seen.add(key)
                picked.append(rec)

                if limit and len(picked) >= limit:
                    break

        return picked
    except Exception as e:
        print(f"[Archive] JSONL 폴백 읽기 실패: {e}")
        return []

# ──────────────────────────────────────────────────────────────
# 퍼블릭 API
# ──────────────────────────────────────────────────────────────
def collect_raw_items(
    *,
    min_importance: float = CAIA_IMPORTANCE_BASE,
    window_hours: int = MERGE_WINDOW_HOURS,
    topic: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    지난 window_hours 시간 동안의 Raw 메시지를 수집.
    1) RAM Raw 우선 수집 → 2) 비어 있으면 JSONL 폴백 (RAW_BACKUP_PATH)
    """
    since_ts = _since_iso(window_hours)

    # 1) RAM
    items = _collect_from_ram(
        min_importance=min_importance,
        since_ts=since_ts,
        topic=topic,
        limit=limit,
    )

    # 2) 폴백(JSONL)
    if not items and RAW_BACKUP_PATH:
        items = _collect_from_jsonl_backup(
            path=RAW_BACKUP_PATH,
            min_importance=min_importance,
            since_ts=since_ts,
            topic=topic,
            limit=limit,
        )

    return items

def write_archive_file(items: List[Dict[str, Any]], *, suffix: str = "") -> str:
    """
    items를 JSON 파일로 저장하고 파일 경로 반환.
    """
    _ensure_dir(ARCHIVE_DIR)
    ts = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    name = f"archive_{ts}{('_' + suffix) if suffix else ''}.json"
    path = os.path.join(ARCHIVE_DIR, name)

    payload = {
        "created_at": _now_iso(),
        "window_hours": MERGE_WINDOW_HOURS,
        "min_importance": CAIA_IMPORTANCE_BASE,
        "count": len(items),
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path

def run_archive_job(
    *,
    min_importance: float = CAIA_IMPORTANCE_BASE,
    window_hours: int = MERGE_WINDOW_HOURS,
    topic: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    스케줄러가 호출할 엔트리포인트.
    지난 window_hours 동안의 Raw를 수집 → 파일로 저장.
    폴백 포함 경로로 수집해 비어 있는 아카이브를 방지.
    """
    since_ts = (datetime.utcnow() - timedelta(hours=window_hours)).isoformat()

    # 1) RAM 수집
    mem = CaiaMemory(session_id="caia-session")
    items = mem.list_raw(
        min_importance=min_importance,
        since_ts=since_ts,
        topic=topic,
        limit=limit,
    )

    # 2) 폴백(JSONL)
    if not items and RAW_BACKUP_PATH:
        items = _collect_from_jsonl_backup(
            path=RAW_BACKUP_PATH,
            min_importance=min_importance,
            since_ts=since_ts,
            topic=topic,
            limit=limit,
        )

    # 저장
    path = write_archive_file(items)
    return {"ok": True, "archived": len(items), "path": path}

# ──────────────── CLI 실행 지원 ────────────────
if __name__ == "__main__":
    res = run_archive_job()
    print(json.dumps(res, ensure_ascii=False, indent=2))
