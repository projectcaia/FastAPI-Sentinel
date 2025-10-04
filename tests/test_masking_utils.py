"""Unit tests for the reusable masking helpers."""

from urllib.parse import parse_qs, urlparse

from utils import mask_secret as exported_mask_secret
from utils.masking import (
    SENSITIVE_KEYS,
    mask_secret,
    redact_dict,
    redact_headers,
    redact_kv,
    redact_ws_url,
)


def test_mask_secret_standard_pattern():
    """Ensure mask_secret keeps 4-prefix, masked middle, and 2-suffix pattern."""
    secret = "abcdefghijklmnop"

    masked = mask_secret(secret)

    assert masked.startswith(secret[:4])
    assert masked.endswith(secret[-2:])
    assert "***" in masked
    # 마스킹된 문자열은 중간에만 별표가 포함되어야 한다
    assert masked.count("*") == 3


def test_mask_secret_handles_short_or_empty_values():
    """Short or empty secrets should collapse entirely into mask token."""
    assert mask_secret("abc") == "***"
    assert mask_secret("") == "***"
    assert mask_secret(None) == "***"


def test_redact_kv_applies_to_sensitive_keys():
    """Sensitive keys must trigger the redaction pipeline."""
    original = "secret-token-value"

    masked = redact_kv("token", original)

    assert masked != original
    assert masked.startswith(original[:4])
    assert masked.endswith(original[-2:])


def test_redact_kv_leaves_non_sensitive_keys():
    """Non-sensitive keys should remain unchanged."""
    value = "public_value"

    assert redact_kv("username", value) == value


def test_redact_ws_url_redacts_sensitive_query_params():
    """WebSocket URLs with sensitive query params should be masked."""
    original_url = "wss://example.com/ws?token=abcdef123456&appkey=xyz987654"

    redacted_url = redact_ws_url(original_url)

    original_params = parse_qs(urlparse(original_url).query)
    redacted_params = parse_qs(urlparse(redacted_url).query)

    assert redacted_params["token"][0] != original_params["token"][0]
    assert redacted_params["token"][0].startswith(original_params["token"][0][:4])
    assert redacted_params["token"][0].endswith(original_params["token"][0][-2:])
    assert redacted_params["appkey"][0] != original_params["appkey"][0]


def test_redact_headers_masks_only_sensitive_values():
    """Authorization headers should be masked while others remain intact."""
    headers = {
        "Authorization": "Bearer super-secret-token",
        "Content-Type": "application/json",
    }

    redacted = redact_headers(headers)

    assert redacted["Authorization"] != headers["Authorization"]
    assert redacted["Authorization"].startswith("Bear")
    assert redacted["Authorization"].endswith("en")
    assert redacted["Content-Type"] == headers["Content-Type"]


def test_redact_dict_recurses_through_nested_structures():
    """Nested dict/list/tuple structures should be redacted recursively."""
    payload = {
        "token": "abcdef123456",
        "metadata": {
            "password": "super-secret",
            "public": "value",
        },
        "records": [
            {"appsecret": "another-secret"},
            "safe",
        ],
        "pair": (
            "entry",
            {"sentinel_key": "sentinel-secret"},
        ),
    }

    redacted = redact_dict(payload)

    assert redacted["token"] != payload["token"]
    assert redacted["metadata"]["password"] != payload["metadata"]["password"]
    assert redacted["metadata"]["public"] == payload["metadata"]["public"]
    assert redacted["records"][0]["appsecret"] != payload["records"][0]["appsecret"]
    assert redacted["records"][1] == "safe"
    assert redacted["pair"][0] == "entry"
    assert redacted["pair"][1]["sentinel_key"] != payload["pair"][1]["sentinel_key"]


def test_utils_package_reexports_masking_helpers():
    """The utils package should expose the masking helpers for reuse."""
    assert exported_mask_secret is mask_secret
    assert all(key.lower() in SENSITIVE_KEYS for key in ["token", "appkey"])
