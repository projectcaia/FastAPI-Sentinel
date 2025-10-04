"""Utility helpers for masking sensitive values in logs and telemetry."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 민감한 키 식별을 위한 기준 목록
SENSITIVE_KEYS: Sequence[str] = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "appkey",
    "app_key",
    "appsecret",
    "app_secret",
    "appsecretkey",
    "authorization",
    "bearer",
    "signature",
    "sentinel_key",
    "connector_secret",
)


def _is_sensitive_key(key: str) -> bool:
    """Return True when the provided key represents a sensitive value."""
    key_lower = key.lower()
    return any(sensitive in key_lower for sensitive in SENSITIVE_KEYS)


def mask_secret(value: Any, head: int = 4, tail: int = 2) -> str:
    """Mask sensitive values with a fixed 4-***-2 visibility pattern."""
    mask_token = "***"

    if value is None:
        return mask_token

    try:
        # 문자열화 후 마스킹 처리
        if isinstance(value, bytes):
            value_str = value.decode("utf-8", "ignore")
        else:
            value_str = str(value)
    except Exception:  # pragma: no cover - 방어적 처리
        return mask_token

    cleaned = value_str.strip()
    if not cleaned:
        return mask_token

    if head < 0 or tail < 0:
        return mask_token

    visible_head = head if head > 0 else 0
    visible_tail = tail if tail > 0 else 0
    threshold = visible_head + visible_tail + len(mask_token)

    if len(cleaned) <= threshold:
        return mask_token

    return f"{cleaned[:4]}{mask_token}{cleaned[-2:]}"


def redact_kv(
    key: str,
    value: Any,
    prefix_visible: int = 4,
    suffix_visible: int = 2,
) -> Any:
    """Redact the provided key/value pair when the key is sensitive."""
    if _is_sensitive_key(key):
        return mask_secret(
            value,
            head=prefix_visible,
            tail=suffix_visible,
        )
    return value


def redact_ws_url(
    url: str,
    prefix_visible: int = 4,
    suffix_visible: int = 2,
) -> str:
    """Redact sensitive query parameters contained in a WebSocket URL."""
    if not url:
        return url

    parsed = urlparse(url)
    if not parsed.query:
        return url

    # 쿼리 파라미터 마스킹
    redacted_params = [
        (
            key,
            redact_kv(
                key,
                value,
                prefix_visible=prefix_visible,
                suffix_visible=suffix_visible,
            ),
        )
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    redacted_query = urlencode(redacted_params)
    return urlunparse(parsed._replace(query=redacted_query))


def redact_headers(
    headers: Mapping[str, Any],
    prefix_visible: int = 4,
    suffix_visible: int = 2,
) -> Dict[str, Any]:
    """Return a copy of headers with sensitive values masked."""
    return {
        key: redact_kv(
            key,
            value,
            prefix_visible=prefix_visible,
            suffix_visible=suffix_visible,
        )
        for key, value in headers.items()
    }


def redact_dict(
    data: Any,
    prefix_visible: int = 4,
    suffix_visible: int = 2,
) -> Any:
    """Recursively redact sensitive keys within a mapping or iterable structure."""
    if isinstance(data, Mapping):
        return {
            key: redact_dict(
                value,
                prefix_visible=prefix_visible,
                suffix_visible=suffix_visible,
            )
            if not _is_sensitive_key(key)
            else mask_secret(
                value,
                head=prefix_visible,
                tail=suffix_visible,
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [
            redact_dict(
                item,
                prefix_visible=prefix_visible,
                suffix_visible=suffix_visible,
            )
            for item in data
        ]
    if isinstance(data, tuple):
        return tuple(
            redact_dict(
                item,
                prefix_visible=prefix_visible,
                suffix_visible=suffix_visible,
            )
            for item in data
        )
    if isinstance(data, set):
        return {
            redact_dict(
                item,
                prefix_visible=prefix_visible,
                suffix_visible=suffix_visible,
            )
            for item in data
        }
    return data


__all__ = [
    "SENSITIVE_KEYS",
    "mask_secret",
    "redact_kv",
    "redact_ws_url",
    "redact_headers",
    "redact_dict",
]
