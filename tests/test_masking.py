"""Unit tests for masking helpers used throughout the project."""
from urllib.parse import parse_qs, urlparse

import pytest

from utils.masking import mask_secret, redact_dict, redact_headers, redact_ws_url


class TestMaskSecret:
    """Validate the core mask_secret behaviour including guard rails."""

    def test_mask_secret_returns_mask_for_none(self):
        """Ensure None input is masked by the default token."""
        assert mask_secret(None) == "***"

    def test_mask_secret_masks_short_values(self):
        """Ensure short strings trigger the guard and return the mask token."""
        assert mask_secret("abcd123") == "***"

    def test_mask_secret_masks_threshold_length(self):
        """Ensure threshold-length secrets are not partially exposed."""
        guarded = mask_secret("abcd123xy")
        assert guarded == "***"

    def test_mask_secret_masks_bytes_payload(self):
        """Ensure bytes payloads are decoded then masked using the pattern."""
        secret = b"abcd123456789"
        masked = mask_secret(secret)
        assert masked.startswith("abcd")
        assert masked.endswith("89")
        assert masked[4:7] == "***"

    @pytest.mark.parametrize(
        "value",
        ["", "  ", []],
    )
    def test_mask_secret_handles_empty_like_values(self, value):
        """Ensure empty or blank-like values collapse to the mask token."""
        assert mask_secret(value) == "***"


class TestRedactionHelpers:
    """Validate helpers that redact sensitive information in collections."""

    def test_redact_ws_url_masks_sensitive_query_parameters(self):
        """Ensure sensitive query params are masked within WebSocket URLs."""
        url = "wss://example.com/socket?token=abcd123456&user_id=42"
        redacted = redact_ws_url(url)
        parsed = urlparse(redacted)
        params = parse_qs(parsed.query)

        assert params["token"][0].startswith("abcd")
        assert params["token"][0].endswith("56")
        assert params["token"][0][4:7] == "***"
        assert params["user_id"][0] == "42"

    def test_redact_ws_url_without_query_returns_original(self):
        """Ensure URLs without queries remain untouched."""
        url = "wss://example.com/socket"
        assert redact_ws_url(url) == url

    def test_redact_headers_masks_sensitive_values(self):
        """Ensure headers containing sensitive keys are masked."""
        headers = {
            "Authorization": "Bearer abcd123456",
            "X-Trace-Id": "trace-001",
        }

        redacted = redact_headers(headers)

        assert redacted["Authorization"].startswith("Bear")
        assert redacted["Authorization"].endswith("56")
        assert redacted["Authorization"][4:7] == "***"
        assert redacted["X-Trace-Id"] == "trace-001"

    def test_redact_dict_masks_nested_structures(self):
        """Ensure nested mappings and sequences are recursively masked."""
        payload = {
            "metadata": {"token": "abcd123456"},
            "headers": {"Authorization": "Token qwerty098765"},
            "values": [
                {"password": "secretvalue", "app_key": "1234"},
                "public",
            ],
            "set_values": {"api_key": "setsecretvalue", "note": "keep"},
            "tuple_values": (
                {"signature": "sigsecretvalue"},
                "transparent",
            ),
        }

        redacted = redact_dict(payload)

        assert redacted["metadata"]["token"].startswith("abcd")
        assert redacted["metadata"]["token"].endswith("56")
        assert redacted["metadata"]["token"][4:7] == "***"
        assert redacted["headers"]["Authorization"].startswith("Toke")
        assert redacted["headers"]["Authorization"].endswith("65")
        assert redacted["headers"]["Authorization"][4:7] == "***"
        assert redacted["values"][0]["password"] == "secr***ue"
        assert redacted["values"][0]["app_key"] == "***"
        assert redacted["values"][1] == "public"
        masked_set = redacted["set_values"]
        assert masked_set["api_key"].startswith("sets")
        assert masked_set["api_key"][4:7] == "***"
        assert masked_set["api_key"].endswith("ue")
        assert masked_set["note"] == "keep"
        masked_tuple = redacted["tuple_values"]
        assert masked_tuple[0]["signature"].startswith("sigs")
        assert masked_tuple[0]["signature"][4:7] == "***"
        assert masked_tuple[0]["signature"].endswith("ue")
        assert masked_tuple[1] == "transparent"
