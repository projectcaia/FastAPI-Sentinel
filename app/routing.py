from typing import Dict, Any
from .utils import summarize, fmt_metrics

def reflex_decide(payload: Dict[str, Any]) -> dict:
    level = payload.get("level", "NA")
    index = payload.get("index", "NA")
    rule = payload.get("rule", "NA")
    metrics_fmt = fmt_metrics(payload.get("metrics", {}))
    summary = summarize(payload)
    return {
        "summary": summary,
        "level": level,
        "index": index,
        "rule": rule,
        "metrics_fmt": metrics_fmt,
    }
