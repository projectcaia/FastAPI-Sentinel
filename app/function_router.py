from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import os, hmac, hashlib, json, re

from .storage import Storage, normalize_text, sha256_hex

router = APIRouter()

# ----- Models -----
class IngestBody(BaseModel):
    text: str = Field(..., description="원문 자연어 텍스트")
    context: str | None = None
    tags: list[str] | None = None
    type: str | None = None
    when: str | None = None

class Record(BaseModel):
    id: str
    type: str
    collection: str
    when: str
    context: str | None = None
    what: str
    insight: str | None = None
    decision: str | None = None
    why: str | None = None
    tags: list[str] = []
    recall_hooks: list[str] = []
    digest_hash: str
    created_at: str
    expires_at: str | None = None

# ----- Helpers -----
def choose_collection(payload: dict) -> str:
    tags = set((payload.get("tags") or []))
    text = (payload.get("text") or "").lower()
    t = (payload.get("type") or "").lower()

    # L1 priority if decision-ish
    if any(k in tags for k in ["decision", "feedback", "draft", "rule", "l1-priority"]) or "decision" in text or t == "memory.digest":
        return "caia_digest"
    # Snapshot if learning/insight only
    if "snapshot" in t or "learn" in t or "memory.snapshot" in t or "memory.learn" in t:
        return "caia_snapshot"
    # Otherwise logs
    return "caia_logs"

def extract_8_slots(text: str) -> dict:
    # Lightweight heuristics; user can refine later
    when = datetime.now(timezone.utc).isoformat()
    what = text.strip()
    insight = None
    decision = None
    why = None
    recall = []

    # Simple rule mining
    if "→" in text or "->" in text:
        parts = re.split(r"→|->", text)
        if len(parts) >= 2:
            what = parts[0].strip()
            decision = parts[-1].strip()

    for kw in ["vix","lv2","hedge","alpha","core","snapshot","digest","rule"]:
        if kw in text.lower():
            recall.append(kw)

    return {
        "when": when,
        "what": what,
        "insight": insight,
        "decision": decision,
        "why": why,
        "recall_hooks": recall
    }

def hmac_ok(raw_body: bytes, signature: str) -> bool:
    secret = os.environ.get("HMAC_SECRET", "")
    if not secret:
        return False
    mac = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature or "")

# ----- Endpoints -----

@router.get("/memory-rules/health")
async def memory_rules_health():
    return {"ok": True, "component": "function_router", "time": datetime.utcnow().isoformat()}

@router.post("/memory-rules/ingest")
async def memory_rules_ingest(body: IngestBody):
    storage = Storage.from_env()
    slots = extract_8_slots(body.text)
    when = body.when or slots["when"]
    nowz = datetime.now(timezone.utc).isoformat()
    norm = normalize_text(body.text)
    dh = sha256_hex(norm)
    collection = choose_collection(body.dict())

    rec = Record(
        id=f"MEM-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        type=(body.type or "memory.learn"),
        collection=collection,
        when=when,
        context=body.context,
        what=slots["what"],
        insight=slots["insight"],
        decision=slots["decision"],
        why=slots["why"],
        tags=body.tags or [],
        recall_hooks=slots["recall_hooks"],
        digest_hash=dh,
        created_at=nowz,
        expires_at=None
    ).dict()

    status, merged_id = storage.upsert(rec)
    summary = (rec["what"][:200] + ("..." if len(rec["what"]) > 200 else ""))
    return {"ok": True, "status": status, "id": merged_id, "collection": collection, "summary": summary}

@router.post("/bridge/ingest")
async def bridge_ingest(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Signature","")
    if not hmac_ok(raw, sig):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    try:
        payload = json.loads(raw.decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Proxy to internal ingest logic
    body = IngestBody(**payload)
    return await memory_rules_ingest(body)

@router.get("/memory/{rid}")
async def memory_get(rid: str):
    storage = Storage.from_env()
    rec = storage.get_by_id(rid)
    if not rec:
        raise HTTPException(status_code=404, detail="not found")
    return rec
