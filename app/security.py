import hmac, hashlib
from typing import Optional

def compute_hmac_sha256(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode('utf-8'), raw, hashlib.sha256).hexdigest()

def verify_hmac(raw: bytes, secret: str, signature_header: Optional[str]) -> bool:
    if not secret or not signature_header:
        return False
    expected = compute_hmac_sha256(raw, secret)
    try:
        return hmac.compare_digest(expected, signature_header.strip())
    except Exception:
        return False
