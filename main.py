"""
This is a patched version of the Sentinel hub forwarder.

Changes:
- Sanitize the idempotency key so that only ASCII characters are used. HTTP
  headers must be ASCII‑compatible, and the original code would include
  non‑ASCII characters (e.g., Δ from ΔVIX) directly in the idempotency key.
  This patch removes any non‑alphanumeric characters from the `index` value
  before building the idempotency key. The sanitized key is used both in
  the request body and in the `Idempotency‑Key` header.

Usage:
Replace the original `main.py` in the FastAPI‑Sentinel repository with this
file and redeploy. The rest of the application remains unchanged.
"""

import os
import hmac
import hashlib
import json
import asyncio
import re

try:
    import httpx  # ensure in requirements
except Exception:
    httpx = None

from datetime import datetime, timezone
import logging

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

HUB_URL = os.getenv("HUB_URL", "").strip()
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "").strip()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger("sentinel-patch")

def _sanitize_index(idx: str) -> str:
    """Return an ASCII‑safe version of the index for the idempotency key.

    HTTP headers allow only ISO‑8859‑1/ASCII characters. When the index
    includes non‑ASCII characters (e.g., Δ), attempting to use it directly in
    the `Idempotency‑Key` header will raise a UnicodeEncodeError. This helper
    removes all characters except alphanumerics, underscores and hyphens.

    Args:
        idx: The original index from the alert payload.

    Returns:
        A sanitized string suitable for use in HTTP headers.
    """
    if not idx:
        return "unknown"
    # Keep ASCII alphanumeric characters, underscores and hyphens
    return re.sub(r'[^A-Za-z0-9_-]', '', idx)


async def _forward_to_hub(raw: bytes, idem_key: str | None = None) -> None:
    """Forward a Sentinel alert to ConnectorHub.

    The function reads a raw JSON alert, constructs a Hub‑compatible
    payload, calculates an HMAC signature, and sends the payload to the Hub.
    To avoid Unicode errors in HTTP headers, the idempotency key is sanitized
    via `_sanitize_index`.
    """
    if not HUB_URL or not CONNECTOR_SECRET:
        log.warning("Hub forward skipped: missing HUB_URL or CONNECTOR_SECRET")
        return

    if httpx is None:
        log.warning("Hub forward skipped: httpx not available")
        return

    try:
        # Parse original sentinel data (UTF‑8 JSON)
        original_data = json.loads(raw.decode("utf-8"))

        # Generate idempotency_key if not provided
        if not idem_key:
            ts = original_data.get("triggered_at", datetime.now(timezone.utc).isoformat())
            idx_raw = original_data.get("index", "unknown")
            # Sanitize index to remove non‑ASCII chars (e.g., Δ)
            idx_safe = _sanitize_index(str(idx_raw))
            # Remove delimiters from timestamp for key uniqueness
            ts_clean = ts.replace(":", "").replace("-", "").replace(".", "").replace("+", "")[:14]
            idem_key = f"SN-{idx_safe}-{ts_clean}"
        else:
            # If a key is provided, sanitize it as a precaution
            idem_key = _sanitize_index(str(idem_key))

        # Create Hub-compatible payload structure
        hub_payload = {
            "idempotency_key": idem_key,  # REQUIRED
            "source": "sentinel",
            "type": "alert.market",
            "priority": "medium",
            "timestamp": original_data.get("triggered_at", datetime.now(timezone.utc).isoformat()),
            "payload": {
                "index": original_data.get("index"),
                "level": original_data.get("level"),
                "delta_pct": original_data.get("delta_pct"),
                "note": original_data.get("note"),
                "original_ts": original_data.get("triggered_at")
            }
        }

        # Serialize the Hub‑formatted data without forcing ASCII (body can be UTF‑8)
        hub_body = json.dumps(hub_payload, ensure_ascii=False).encode("utf-8")

        # Calculate HMAC signature for the Hub‑formatted body
        sig = hmac.new(CONNECTOR_SECRET.encode(), hub_body, hashlib.sha256).hexdigest()

        # Headers with signature and sanitized idempotency key
        headers = {
            "Content-Type": "application/json",
            "X-Signature": sig,
            "Idempotency-Key": idem_key  # Hub checks both header and body
        }

        log.info("Hub forward attempt: %s → %s", idem_key, HUB_URL)

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(HUB_URL, content=hub_body, headers=headers)
            if response.status_code == 200:
                log.info("Hub forward success: %s", idem_key)
            else:
                log.warning("Hub forward failed: %d - %s", response.status_code, response.text)

    except Exception as e:
        # Log any exceptions; httpx will include Unicode errors if they occur
        log.error("Hub forward error: %s", str(e))


# -----------------------------------------------------------------------------
# Note: The remainder of the original main.py (FastAPI routes, state management,
# etc.) has been omitted in this patch since it remains unchanged. When
# integrating, retain the rest of the original file content.
# -----------------------------------------------------------------------------