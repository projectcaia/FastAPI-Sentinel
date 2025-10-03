"""
DB증권 Token Manager (최종 통합 버전)
- Query-string 방식(scope=oob 기본) 포함
- Auto refresh loop 유지
- Fallback: form → json 순서 시도
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import httpx
import json

logger = logging.getLogger(__name__)


class DBSecTokenManager:
    """DB증권 API token manager with automatic refresh"""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        base_url: str = "https://openapi.dbsec.co.kr:8443",
        scope: str = "oob",
        enabled: bool = True,
    ):
        self.app_key = app_key.strip()
        self.app_secret = app_secret.strip()
        self.scope = scope.strip() if scope else "oob"
        self.enabled = enabled

        self.base_url = base_url.rstrip("/")
        self.token_url = f"{self.base_url}/oauth2/token"

        self.access_token: Optional[str] = None
        self.token_type: str = "Bearer"
        self.expires_at: Optional[datetime] = None

        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        self._last_request_time: Optional[datetime] = None
        self._consecutive_failures = 0
        self._backoff_seconds = 60

        if self.enabled:
            logger.info(f"[DBSEC] Token Manager ENABLED (scope={self.scope})")
        else:
            logger.warning("[DBSEC] Token Manager DISABLED - mock mode")
            self.access_token = "MOCK_TOKEN_DISABLED"
            self.expires_at = datetime.now(timezone.utc) + timedelta(days=365)

    async def get_token(self) -> Optional[str]:
        """Get current valid access token"""
        if not self.enabled:
            return self.access_token

        async with self._lock:
            if self._is_token_valid():
                return self.access_token

            if self._last_request_time:
                since_last = (datetime.now(timezone.utc) - self._last_request_time).total_seconds()
                if since_last < 30:
                    await asyncio.sleep(30 - since_last)

            success = await self._refresh_token()
            if success:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                logger.error(f"[DBSEC] Token refresh failed ({self._consecutive_failures})")

            return self.access_token

    def _is_token_valid(self) -> bool:
        if not self.access_token or not self.expires_at:
            return False
        return self.expires_at > datetime.now(timezone.utc) + timedelta(minutes=5)

    async def _refresh_token(self) -> bool:
        """Try DB증권 token request (query → form → json 순서)"""
        self._last_request_time = datetime.now(timezone.utc)
        timeout = httpx.Timeout(30.0)

        attempts = [
            {
                "mode": "query",
                "url": (
                    f"{self.token_url}"
                    f"?appkey={self.app_key}"
                    f"&appsecretkey={self.app_secret}"
                    f"&grant_type=client_credentials"
                    f"&scope={self.scope}"
                ),
                "kwargs": {"headers": {"Accept": "application/json"}},
            },
            {
                "mode": "form",
                "url": self.token_url,
                "kwargs": {
                    "headers": {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    "data": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecretkey": self.app_secret,
                        "scope": self.scope,
                    },
                },
            },
            {
                "mode": "json",
                "url": self.token_url,
                "kwargs": {
                    "headers": {
                        "Content-Type": "application/json; charset=UTF-8",
                        "Accept": "application/json",
                    },
                    "json": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecretkey": self.app_secret,
                        "scope": self.scope,
                    },
                },
            },
        ]

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for attempt in attempts:
                    mode = attempt["mode"]
                    logger.info(f"[DBSEC] Trying token request mode={mode}")
                    resp = await client.post(attempt["url"], **attempt["kwargs"])

                    if resp.status_code == 200:
                        token_data = resp.json()
                        self.access_token = token_data.get("access_token")
                        self.token_type = token_data.get("token_type", "Bearer")
                        exp = token_data.get("expires_in", 86400)
                        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=exp)
                        logger.info(f"[DBSEC] Token success (mode={mode}), expires_in={exp}s")
                        return True
                    else:
                        try:
                            err = resp.json()
                            logger.error(f"[DBSEC] {mode} failed {resp.status_code} {err}")
                        except:
                            logger.error(f"[DBSEC] {mode} failed {resp.status_code} {resp.text}")

            return False

        except Exception as e:
            logger.error(f"[DBSEC] Token refresh exception: {e}")
            return False

    async def start_auto_refresh(self):
        if not self.enabled:
            return
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())

    async def stop_auto_refresh(self):
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _auto_refresh_loop(self):
        try:
            token = await self.get_token()
            if not token:
                logger.warning("[DBSEC] Initial token fetch failed")

            while True:
                if self._is_token_valid():
                    await asyncio.sleep(23 * 3600)
                else:
                    await asyncio.sleep(self._backoff_seconds)
                await self._refresh_token()
        except asyncio.CancelledError:
            pass

    def get_auth_header(self) -> Dict[str, str]:
        if not self.access_token:
            return {}
        return {"Authorization": f"{self.token_type} {self.access_token}"}


# Global instance
_token_manager: Optional[DBSecTokenManager] = None


def get_token_manager() -> DBSecTokenManager:
    global _token_manager
    if _token_manager is None:
        enabled = os.getenv("DBSEC_ENABLE", "true").lower() in ["true", "1", "yes"]
        app_key = os.getenv("DB_APP_KEY", "")
        app_secret = os.getenv("DB_APP_SECRET", "")
        scope = os.getenv("DB_SCOPE", "oob")
        base_url = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443")
        _token_manager = DBSecTokenManager(app_key, app_secret, base_url, scope, enabled)
    return _token_manager

async def init_token_manager():
    """Initialize and start the global token manager"""
    manager = get_token_manager()
    if manager:
        await manager.start_auto_refresh()
        if manager.enabled:
            logger.info("[DBSEC] Token Manager initialized in PRODUCTION mode")
        else:
            logger.info("[DBSEC] Token Manager initialized in MOCK mode")


async def shutdown_token_manager():
    """Shutdown the global token manager"""
    global _token_manager
    if _token_manager:
        await _token_manager.stop_auto_refresh()
        _token_manager = None
        logger.info("[DBSEC] Token Manager shutdown")
