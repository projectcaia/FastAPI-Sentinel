from typing import Literal, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator
from datetime import datetime

Priority = Literal["normal", "high", "urgent"]

class PayloadMetrics(BaseModel):
    dK200: float
    dVIX: float

class IngestPayload(BaseModel):
    rule: str
    index: str
    level: str
    metrics: PayloadMetrics

class IngestModel(BaseModel):
    idempotency_key: str = Field(..., min_length=1)
    source: Literal["sentinel"]
    type: Literal["alert.market"]
    priority: Priority
    timestamp: str
    payload: IngestPayload

    @field_validator("timestamp")
    @classmethod
    def ts_is_iso(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception as e:
            raise ValueError("timestamp must be ISO-8601") from e
        return v

class JobRecord(BaseModel):
    id: Optional[int] = None
    idempotency_key: str
    source: str
    type: str
    priority: str
    timestamp: str
    payload_json: Dict[str, Any]
    ack: Optional[str] = None
    job_url: Optional[str] = None
    dedup: bool = False
    status: str = "queued"
    retries: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
