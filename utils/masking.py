"""Utility helpers for masking sensitive values in logs and telemetry."""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

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


def mask_secret(value: Any, prefix_visible: int = 4, suffix_visible: int = 2) -> str:
    """Mask sensitive values using a 4-***-2 style pattern."""
    if value is None:
        return "***"

    try:
        # 문자열화 후 마스킹 처리
        if isinstance(value, bytes):
            value_str = value.decode("utf-8", "ignore")
        else:
            value_str = str(value)
    except Exception as error:  # pragma: no cover - 방어적 로깅
        logger.warning("Failed to normalize secret for masking: %s", error)
        return "***"

    cleaned = value_str.strip()
    if not cleaned:
        return "***"

    if prefix_visible <= 0 or suffix_visible <= 0:
        return "***"

    mask_token = "***"

    if len(cleaned) <= prefix_visible + suffix_visible + len(mask_token):
        return mask_token

    return f"{cleaned[:prefix_visible]}{mask_token}{cleaned[-suffix_visible:]}"


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
            prefix_visible=prefix_visible,
            suffix_visible=suffix_visible,
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
                prefix_visible=prefix_visible,
                suffix_visible=suffix_visible,
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
