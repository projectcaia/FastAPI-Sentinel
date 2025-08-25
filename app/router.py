from fastapi import APIRouter, Request, HTTPException, Query
from typing import Optional
import json, logging
from datetime import datetime, timezone

from .config import VERSION, CONNECTOR_SECRET, BASE_URL, PUSH_SIMULATE_429
from .security import verify_hmac
from .models import IngestModel
from . import db
from .routing import reflex_decide
from .utils import gen_ack

router = APIRouter()
log = logging.getLogger(__name__)

@router.get("/ready")
async def ready():
    utc_now = datetime.now(timezone.utc).isoformat()
    return {"ok": True, "version": VERSION, "utc_now": utc_now}

@router.get("/health")
async def health():
    con = db.connect()
    db.migrate(con)
    errors = db.errors_count(con, hours=24)
    return {"ok": True, "detail": {"errors_24h": errors}}

@router.get("/jobs")
async def jobs(hours: int = Query(24, ge=1, le=168), limit: int = Query(50, ge=1, le=500)):
    con = db.connect()
    items = db.recent_jobs(con, hours=hours, limit=limit)
    return {"ok": True, "items": items}

@router.post("/bridge/ingest")
async def ingest(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Signature")
    if not verify_hmac(raw, CONNECTOR_SECRET, sig):
        log.error("HMAC verification failed", extra={"event":"hmac_fail","route":"/bridge/ingest"})
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=422, detail="invalid json")
    try:
        model = IngestModel.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"schema error: {e}")
    idem = request.headers.get("Idempotency-Key") or model.idempotency_key
    con = db.connect()
    db.migrate(con)
    rec = {
        "idempotency_key": idem,
        "source": model.source,
        "type": model.type,
        "priority": model.priority,
        "timestamp": model.timestamp,
        "payload_json": data.get("payload"),
        "status": "queued",
    }
    job_id, duplicate = db.insert_job(con, rec)
    if duplicate:
        db.add_event(con, job_id or 0, "dedup", "duplicate idempotency_key", {"idempotency_key": idem})
        return {"ok": True, "dedup": True, "job_id": job_id}
    db.add_event(con, job_id, "ingest", "accepted", {"idempotency_key": idem})
    route_res = reflex_decide(data.get("payload", {}))
    db.add_event(con, job_id, "route", "reflex_decide", route_res)
    ack = gen_ack()
    job_url = f"{BASE_URL}/jobs/{idem}" if BASE_URL else f"http://localhost:8080/jobs/{idem}"
    text_lines = [
        f"[Sentinel/{route_res['level']}] {route_res['index']} {route_res['rule']}",
        f"rule={route_res['rule']} index={route_res['index']} level={route_res['level']} priority={model.priority}",
        route_res["metrics_fmt"],
        f"job: {job_url}",
        f"ACK: {ack}",
        f"Copy for Caia: 센티넬 반영 {ack}",
    ]
    message = "\n".join(text_lines)
    from .push import send_telegram_message
    simulate_429 = (request.headers.get("X-Debug-TG429", "0") == "1") or PUSH_SIMULATE_429
    result = await send_telegram_message(message, simulate_429=simulate_429, max_retries=3)
    dispatched = bool(result.get("ok"))
    status = "pushed" if dispatched else "failed"
    retries = result.get("attempts", 0)
    db.update_job_push(con, job_id, ack=ack, job_url=job_url, status=status, retries=retries)
    db.add_event(con, job_id, "push" if dispatched else "error", "telegram", result)
    return {
        "ok": dispatched,
        "job_id": job_id,
        "ack": ack,
        "dedup": False,
        "status": status,
        "queued": True,
        "dispatched": dispatched,
        "summary_sent": dispatched
    }
