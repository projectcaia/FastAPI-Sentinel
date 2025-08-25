import json, logging, sys
from .config import LOG_LEVEL

def _json_formatter(record: logging.LogRecord) -> str:
    payload = {
        "level": record.levelname,
        "ts": getattr(record, "created", None),
        "event": getattr(record, "event", None),
        "route": getattr(record, "route", None),
        "job_id": getattr(record, "job_id", None),
        "idempotency_key": getattr(record, "idempotency_key", None),
        "status": getattr(record, "status", None),
        "message": record.getMessage(),
    }
    if record.exc_info:
        payload["error"] = {
            "kind": str(record.exc_info[0].__name__),
            "message": str(record.exc_info[1]),
        }
    return json.dumps({k:v for k,v in payload.items() if v is not None}, ensure_ascii=False)

class JsonLogHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = _json_formatter(record)
            self.stream.write(msg + "\n")
            self.flush()
        except Exception:
            super().emit(record)

def setup_logger():
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = JsonLogHandler(stream=sys.stdout)
    root.addHandler(handler)
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
