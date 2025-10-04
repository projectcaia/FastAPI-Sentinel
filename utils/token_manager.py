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
from typing import Optional, Dict, Any, List, Tuple
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
        self._preferred_first_mode: Optional[str] = None
        self._backoff_until: Optional[datetime] = None
        self._max_backoff_seconds = 60
        self._max_gateway_retries = 3

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

            if self._is_in_backoff():
                now = datetime.now(timezone.utc)
                wait_seconds = (self._backoff_until - now).total_seconds() if self._backoff_until else 0
                logger.warning(
                    "[DBSEC] Token refresh skipped due to backoff window %.1fs remaining",
                    max(0, wait_seconds),
                )
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

    def _is_in_backoff(self) -> bool:
        """Return True if IGW backoff window is active."""
        if not self._backoff_until:
            return False
        return datetime.now(timezone.utc) < self._backoff_until

    async def _refresh_token(self) -> bool:
        """Try DB증권 token request (query → form → json 순서)"""
        self._last_request_time = datetime.now(timezone.utc)
        timeout = httpx.Timeout(30.0)

        attempts_by_mode: Dict[str, Dict[str, Any]] = {
            "query": {
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
            "form": {
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
            "json": {
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
        }

        attempt_order = self._get_attempt_order()
        attempts: List[Dict[str, Any]] = [attempts_by_mode[m] for m in attempt_order]

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                pending_modes = attempts.copy()
                while pending_modes:
                    attempt = pending_modes.pop(0)
                    mode = attempt["mode"]
                    retry_count = 0

                    while True:
                        logger.info(
                            "[DBSEC] Trying token request mode=%s retry=%s",
                            mode,
                            retry_count,
                        )
                        resp = await client.post(attempt["url"], **attempt["kwargs"])

                        if resp.status_code == 200:
                            token_data = resp.json()
                            self.access_token = token_data.get("access_token")
                            self.token_type = token_data.get("token_type", "Bearer")
                            exp = token_data.get("expires_in", 86400)
                            self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=exp)
                            self._preferred_first_mode = None
                            self._backoff_until = None
                            logger.info(
                                "[DBSEC] Token success (mode=%s), expires_in=%ss",
                                mode,
                                exp,
                            )
                            return True

                        error_code, payload = self._extract_error_code(resp)
                        if isinstance(payload, (dict, list)):
                            log_payload = payload
                        else:
                            log_payload = payload if payload is not None else resp.text
                        logger.error(
                            "[DBSEC] %s failed %s code=%s payload=%s",
                            mode,
                            resp.status_code,
                            error_code,
                            log_payload,
                        )

                        if error_code == "IGW00133":
                            self._preferred_first_mode = "form"
                            if mode != "form":
                                pending_modes = [attempts_by_mode["form"]]
                                logger.warning(
                                    "[DBSEC] IGW00133 detected, pivoting to form mode and skipping other attempts",
                                )
                            else:
                                pending_modes = []
                                logger.warning(
                                    "[DBSEC] IGW00133 persisted in form mode; skipping remaining attempts",
                                )
                            break

                        if error_code == "IGW00201":
                            handled = await self._handle_gateway_backoff(mode, retry_count)
                            if handled:
                                retry_count += 1
                                continue
                            pending_modes = []
                            return False

                        break

            return False

        except Exception as e:
            logger.error(f"[DBSEC] Token refresh exception: {e}")
            return False

    async def _handle_gateway_backoff(self, mode: str, retry_count: int) -> bool:
        """Handle IGW00201 backoff and retries."""
        if retry_count >= self._max_gateway_retries:
            wait_seconds = min(2 ** (retry_count + 1), self._max_backoff_seconds)
            self._backoff_until = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
            logger.warning(
                "[DBSEC] IGW00201 retry cap reached (mode=%s). Backing off until %s",
                mode,
                self._backoff_until,
            )
            return False

        delay = min(2 ** (retry_count + 1), self._max_backoff_seconds)
        next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        logger.warning(
            "[DBSEC] IGW00201 detected (mode=%s). retry=%s sleeping=%ss next_retry=%s",
            mode,
            retry_count + 1,
            delay,
            next_retry_at,
        )
        await asyncio.sleep(delay)
        return True

    def _get_attempt_order(self) -> List[str]:
        """Determine request mode order applying any preference."""
        base_order = ["query", "form", "json"]
        if self._preferred_first_mode and self._preferred_first_mode in base_order:
            base_order.remove(self._preferred_first_mode)
            return [self._preferred_first_mode] + base_order
        return base_order

    def _extract_error_code(self, response: httpx.Response) -> Tuple[Optional[str], Optional[Any]]:
        """Extract IGW error code and payload from response."""
        payload: Optional[Any] = None
        try:
            payload = response.json()
        except Exception:
            payload = None

        codes = ("IGW00133", "IGW00201")
        if payload is not None:
            serialized = json.dumps(payload, ensure_ascii=False)
            for code in codes:
                if code in serialized:
                    return code, payload

        text = response.text or ""
        for code in codes:
            if code in text:
                return code, payload if payload is not None else text

        return None, payload if payload is not None else response.text

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
                if self._is_in_backoff():
                    now = datetime.now(timezone.utc)
                    wait_seconds = (self._backoff_until - now).total_seconds() if self._backoff_until else 0
                    if wait_seconds > 0:
                        logger.info(
                            "[DBSEC] Auto refresh paused for backoff window %.1fs",
                            wait_seconds,
                        )
                        await asyncio.sleep(wait_seconds)
                        continue
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
