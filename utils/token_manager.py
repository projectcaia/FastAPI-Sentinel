"""
DB증권 Token Manager
Handles OAuth2 token acquisition and refresh for DB증권 Open API
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
        enabled: bool = True
    ):
        # Strip all whitespace including newlines from credentials and URL
        self.app_key = app_key.strip() if app_key else ""
        self.app_secret = app_secret.strip() if app_secret else ""
        self.enabled = enabled
        
        # Clean and validate base URL
        cleaned_base_url = base_url.strip().rstrip("/") if base_url else ""
        if enabled and not cleaned_base_url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid base_url format: {base_url!r}. Must start with http:// or https://")
        
        self.base_url = cleaned_base_url
        self.token_url = f"{self.base_url}/oauth2/token"
        
        # Initialize based on enabled state
        if self.enabled:
            logger.info(f"[DB증권] Token Manager ENABLED - will connect to: {self.base_url}")
            logger.info(f"[DB증권] Token URL: {self.token_url}")
            logger.info(f"[DB증권] App Key configured: {'Yes' if self.app_key else 'No'} (length: {len(self.app_key) if self.app_key else 0})")
            logger.info(f"[DB증권] App Secret configured: {'Yes' if self.app_secret else 'No'} (length: {len(self.app_secret) if self.app_secret else 0})")
            
            self.access_token: Optional[str] = None
            self.token_type: str = "Bearer"
            self.expires_at: Optional[datetime] = None
        else:
            logger.warning("[DB증권] Token Manager DISABLED (mock mode) - no API calls will be made")
            # Use mock token for disabled mode
            self.access_token: Optional[str] = "MOCK_TOKEN_DISABLED"
            self.token_type: str = "Bearer"
            self.expires_at: Optional[datetime] = datetime.now(timezone.utc) + timedelta(days=365)
        
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        
        # Rate limiting and error tracking (only for enabled mode)
        self._last_request_time: Optional[datetime] = None
        self._consecutive_failures = 0
        self._backoff_seconds = 60  # Fixed 60 second retry on failure
        
    async def get_token(self) -> Optional[str]:
        """Get current valid access token"""
        if not self.enabled:
            return self.access_token  # Return mock token
            
        async with self._lock:
            # Check if token is still valid
            if self._is_token_valid():
                return self.access_token
            
            # Check rate limiting (minimum 30 seconds between requests)
            if self._last_request_time:
                time_since_last = datetime.now(timezone.utc) - self._last_request_time
                if time_since_last.total_seconds() < 30:
                    wait_time = 30 - time_since_last.total_seconds()
                    logger.warning(f"[DB증권] Rate limiting: waiting {wait_time:.1f}s until next request")
                    await asyncio.sleep(wait_time)
            
            # Token expired or not exists, refresh it
            success = await self._refresh_token()
            if success:
                self._consecutive_failures = 0
                logger.info("[DB증권] Token acquired successfully")
            else:
                self._consecutive_failures += 1
                logger.error(f"[DB증권] Token refresh failed ({self._consecutive_failures} consecutive failures)")
                logger.info(f"[DB증권] Will retry in {self._backoff_seconds} seconds")
                
            return self.access_token
    
    def _is_token_valid(self) -> bool:
        """Check if current token is valid (not expired)"""
        if not self.enabled:
            return True  # Mock token is always valid
            
        if not self.access_token or not self.expires_at:
            return False
        
        # Add 5 minute buffer before expiration
        buffer_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        return self.expires_at > buffer_time
    
    async def _refresh_token(self) -> bool:
        """Refresh access token from DB증권 API"""
        if not self.enabled:
            logger.debug("[DB증권] Token refresh skipped - mock mode")
            return True

        self._last_request_time = datetime.now(timezone.utc)

        # Try DB증권-preferred formats in order:
        # 1) form-urlencoded with lowercase keys (appkey/appsecret)
        # 2) form-urlencoded with camelCase keys (appKey/appSecret)
        # 3) JSON with camelCase keys (as a last resort)
        attempts = [
            {
                "mode": "FORM-LOWER",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "payload_key": "data",
                "description": "form-urlencoded lowercase mode",
            },
            {
                "mode": "FORM-CAMEL",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "payload_key": "data",
                "description": "form-urlencoded camelCase mode",
            },
            {
                "mode": "JSON",
                "headers": {"Content-Type": "application/json"},
                "payload_key": "json",
                "description": "JSON mode",
            },
        ]

        logger.info(
            f"[DB증권] Preparing token request with grant_type=client_credentials, "
            f"appKey length={len(self.app_key)}, appSecret length={len(self.app_secret)}"
        )

        timeout = httpx.Timeout(30.0)

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                for index, attempt in enumerate(attempts):
                    mode = attempt["mode"]
                    mode_label = attempt["description"]
                    is_json_mode = mode == "JSON"
                    headers = attempt["headers"]
                    # Build payload per mode
                    if mode == "FORM-LOWER":
                        payload = {
                            "grant_type": "client_credentials",
                            "appkey": self.app_key,
                            "appsecret": self.app_secret,
                        }
                    else:
                        payload = {
                            "grant_type": "client_credentials",
                            "appKey": self.app_key,
                            "appSecret": self.app_secret,
                        }
                    request_kwargs = {attempt["payload_key"]: payload}

                    if mode == "FORM-LOWER":
                        logger.info("[DB증권] Attempting token request using form-urlencoded (lowercase) mode")
                    elif mode == "FORM-CAMEL":
                        logger.info("[DB증권] Token request fallback to form-urlencoded (camelCase) mode")
                    else:
                        logger.info("[DB증권] Token request fallback to JSON mode")

                    logger.debug(f"[DB증권] Token endpoint: {self.token_url}")
                    logger.debug(f"[DB증권] Request headers: {headers}")

                    response = await client.post(
                        self.token_url,
                        headers=headers,
                        **request_kwargs,
                    )

                    logger.debug(f"[DB증권] Response status: {response.status_code}")
                    logger.debug(f"[DB증권] Response headers: {dict(response.headers)}")

                    if response.status_code == 200:
                        try:
                            token_data = response.json()
                        except json.JSONDecodeError as exc:
                            logger.error(f"[DB증권] Failed to parse token response: {exc}")
                            logger.error(f"[DB증권] Response text: {response.text[:500]}")
                            return False

                        self.access_token = token_data.get("access_token")
                        expires_in = token_data.get("expires_in", 86400)
                        self.token_type = token_data.get("token_type", "Bearer")

                        if not self.access_token:
                            logger.error("[DB증권] Response missing access_token field")
                            return False

                        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

                        logger.info(f"[DB증권] Token request succeeded using {mode_label}")
                        logger.info(f"[DB증권] Access token acquired, expires in {expires_in}s")
                        logger.debug(f"[DB증권] Token expires at: {self.expires_at}")
                        logger.debug(f"[DB증권] Token type: {self.token_type}")
                        return True

                    if response.status_code == 403:
                        try:
                            error_data = response.json()
                            error_code = error_data.get("error_code", "")
                            error_desc = error_data.get("error_description", response.text)
                        except json.JSONDecodeError:
                            error_code = ""
                            error_desc = response.text

                        if error_code == "IGW00105":
                            logger.error(f"[DB증권] Invalid AppSecret: {error_desc}")
                            logger.error("[DB증권] Please check DB_APP_SECRET environment variable")
                        elif error_code == "IGW00103":
                            logger.error(f"[DB증권] Invalid AppKey: {error_desc}")
                            logger.error("[DB증권] Please check DB_APP_KEY environment variable")
                        elif error_code == "IGW00201":
                            logger.error(f"[DB증권] API call limit exceeded: {error_desc}")
                            logger.error("[DB증권] Wait for quota reset or contact DB증권 support")
                        else:
                            logger.error(f"[DB증권] Auth error {error_code}: {error_desc}")
                    elif response.status_code == 401:
                        logger.error(f"[DB증권] Authentication failed (401): {response.text[:500]}")
                        logger.error("[DB증권] Check if API credentials are valid and active")
                    elif response.status_code == 400:
                        logger.error(f"[DB증권] Bad request (400): {response.text[:500]}")
                        logger.error("[DB증권] Check request format - JSON camelCase or form-urlencoded camelCase fields are supported")
                    else:
                        logger.warning(f"[DB증권] Unexpected response {response.status_code}: {response.text[:500]}")

                    if index < len(attempts) - 1:
                        logger.warning(
                            f"[DB증권] Token request failed using {mode_label} (status {response.status_code}), trying fallback"
                        )
                        continue

                    return False

        except httpx.TimeoutException:
            logger.error("[DB증권] Token request timeout after 30 seconds")
            return False
        except httpx.RequestError as exc:
            logger.error(f"[DB증권] Network error during token request: {exc}")
            return False
        except Exception as exc:
            logger.error(f"[DB증권] Unexpected error during token refresh: {exc}")
            return False

    async def start_auto_refresh(self):
        """Start automatic token refresh task (every 23 hours)"""
        if not self.enabled:
            logger.info("[DB증권] Auto refresh disabled - mock mode")
            return
            
        if self._refresh_task and not self._refresh_task.done():
            return
        
        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
        logger.info("[DB증권] Auto refresh task started")
    
    async def stop_auto_refresh(self):
        """Stop automatic token refresh task"""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("[DB증권] Auto refresh task stopped")
    
    async def _auto_refresh_loop(self):
        """Auto refresh loop - runs every 23 hours"""
        if not self.enabled:
            return
            
        try:
            # Initial token fetch
            token = await self.get_token()
            if not token:
                logger.warning("[DB증권] Initial token fetch failed, will retry")
            
            while True:
                if self._is_token_valid():
                    # Wait until near expiration (23 hours)
                    await asyncio.sleep(23 * 3600)
                else:
                    # Token invalid, retry after backoff
                    await asyncio.sleep(self._backoff_seconds)
                
                # Try to refresh token
                async with self._lock:
                    await self._refresh_token()
                    
        except asyncio.CancelledError:
            logger.info("[DB증권] Auto refresh loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[DB증권] Auto refresh loop error: {e}")
    
    def get_auth_header(self) -> Dict[str, str]:
        """Get authorization header for API requests"""
        if not self.access_token:
            return {}
        return {"Authorization": f"{self.token_type} {self.access_token}"}
    
    async def health_check(self) -> Dict[str, Any]:
        """Check token manager health status"""
        if not self.enabled:
            return {
                "enabled": False,
                "mode": "MOCK",
                "token_valid": True,
                "has_token": True,
                "status": "DISABLED - Mock mode for local development"
            }
            
        return {
            "enabled": True,
            "mode": "PRODUCTION",
            "token_valid": self._is_token_valid(),
            "has_token": bool(self.access_token),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_task_active": bool(self._refresh_task and not self._refresh_task.done()),
            "consecutive_failures": self._consecutive_failures,
            "last_request_time": self._last_request_time.isoformat() if self._last_request_time else None,
            "app_key_length": len(self.app_key) if self.app_key else 0,
            "app_secret_length": len(self.app_secret) if self.app_secret else 0
        }


# Global token manager instance
_token_manager: Optional[DBSecTokenManager] = None


def get_token_manager() -> Optional[DBSecTokenManager]:
    """Get global token manager instance"""
    global _token_manager
    
    if _token_manager is None:
        # Check if DB증권 module is enabled
        dbsec_enabled = os.getenv("DBSEC_ENABLE", "true").lower() in ["true", "1", "yes", "on"]
        
        # Strip whitespace and newlines from environment variables
        app_key = os.getenv("DB_APP_KEY", "").strip()
        app_secret = os.getenv("DB_APP_SECRET", "").strip()
        base_url = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443").strip()
        
        if not dbsec_enabled:
            logger.info("[DB증권] Module DISABLED by DBSEC_ENABLE=false")
        elif not app_key or not app_secret:
            logger.warning("[DB증권] Missing DB_APP_KEY or DB_APP_SECRET - using mock mode")
            dbsec_enabled = False
        else:
            logger.info(f"[DB증권] Initializing with credentials: "
                       f"key={app_key[:4]}...{app_key[-4:] if len(app_key) > 8 else 'SHORT'}, "
                       f"secret={'*' * len(app_secret)}")
        
        try:
            _token_manager = DBSecTokenManager(
                app_key=app_key or "MOCK_KEY",
                app_secret=app_secret or "MOCK_SECRET",
                base_url=base_url,
                enabled=dbsec_enabled
            )
        except ValueError as e:
            logger.error(f"[DB증권] Failed to initialize token manager: {e}")
            return None
    
    return _token_manager


async def init_token_manager():
    """Initialize and start the global token manager"""
    manager = get_token_manager()
    if manager:
        await manager.start_auto_refresh()
        if manager.enabled:
            logger.info("[DB증권] Token Manager initialized in PRODUCTION mode")
        else:
            logger.info("[DB증권] Token Manager initialized in MOCK mode")


async def shutdown_token_manager():
    """Shutdown the global token manager"""
    global _token_manager
    if _token_manager:
        await _token_manager.stop_auto_refresh()
        _token_manager = None
        logger.info("[DB증권] Token Manager shutdown")
