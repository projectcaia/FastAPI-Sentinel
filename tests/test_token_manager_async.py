"""Asynchronous tests for DBSecTokenManager error handling."""

import json
from datetime import datetime, timezone
from typing import Optional

import pytest

from utils.token_manager import DBSecTokenManager


class MockResponse:
    """Simple mock for httpx.Response."""

    def __init__(self, status_code: int, json_data=None, text: Optional[str] = None):
        self.status_code = status_code
        self._json_data = json_data
        if text is not None:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ""

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON body")
        return self._json_data


class MockAsyncClient:
    """Mock AsyncClient to supply queued responses."""

    response_queue = []
    instances = []

    def __init__(self, *args, **kwargs):
        self.requests = []
        MockAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        if not MockAsyncClient.response_queue:
            raise AssertionError("No responses left in queue")
        response = MockAsyncClient.response_queue.pop(0)
        self.requests.append({"url": url, "kwargs": kwargs})
        return response


@pytest.mark.asyncio
async def test_refresh_pivots_to_form_on_igw00133(monkeypatch):
    """Pivot to form mode and persist preference until success."""

    manager = DBSecTokenManager("key", "secret", enabled=True)

    MockAsyncClient.instances = []
    MockAsyncClient.response_queue = [
        MockResponse(400, {"error": {"code": "IGW00133"}}),
        MockResponse(500, {"error": {"message": "fallback"}}),
        MockResponse(
            200,
            {
                "access_token": "token-form",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        ),
    ]
    monkeypatch.setattr("utils.token_manager.httpx.AsyncClient", MockAsyncClient)

    success_first = await manager._refresh_token()
    assert success_first is False
    assert manager._preferred_first_mode == "form"
    assert len(MockAsyncClient.instances[0].requests) == 2
    assert "?appkey=" in MockAsyncClient.instances[0].requests[0]["url"]
    assert "?appkey=" not in MockAsyncClient.instances[0].requests[1]["url"]

    success_second = await manager._refresh_token()
    assert success_second is True
    assert manager.access_token == "token-form"
    assert manager._preferred_first_mode is None
    assert len(MockAsyncClient.instances[1].requests) == 1
    assert "?appkey=" not in MockAsyncClient.instances[1].requests[0]["url"]


@pytest.mark.asyncio
async def test_refresh_backoff_on_igw00201(monkeypatch):
    """Retry IGW00201 up to cap, then enforce safe backoff window."""

    manager = DBSecTokenManager("key", "secret", enabled=True)

    MockAsyncClient.instances = []
    MockAsyncClient.response_queue = [
        MockResponse(429, {"error": {"code": "IGW00201"}}),
        MockResponse(429, {"error": {"code": "IGW00201"}}),
        MockResponse(429, {"error": {"code": "IGW00201"}}),
        MockResponse(429, {"error": {"code": "IGW00201"}}),
    ]
    monkeypatch.setattr("utils.token_manager.httpx.AsyncClient", MockAsyncClient)

    sleep_calls = []

    async def fake_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr("utils.token_manager.asyncio.sleep", fake_sleep)

    success = await manager._refresh_token()
    assert success is False
    assert sleep_calls == [2, 4, 8]
    assert len(MockAsyncClient.instances[0].requests) == 4
    assert manager._backoff_until is not None
    assert manager._is_in_backoff() is True

    remaining = (manager._backoff_until - datetime.now(timezone.utc)).total_seconds()
    assert 0 < remaining <= manager._max_backoff_seconds
