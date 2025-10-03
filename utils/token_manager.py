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
        base_url: str = "https://openapi.dbsec.co.kr:8443"
    ):
        # Strip all whitespace including newlines from credentials and URL
        self.app_key = app_key.strip() if app_key else ""
        self.app_secret = app_secret.strip() if app_secret else ""
        
        # Clean and validate base URL
        cleaned_base_url = base_url.strip().rstrip("/") if base_url else ""
        if not cleaned_base_url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid base_url format: {base_url!r}. Must start with http:// or https://")
        
        # Check for invalid characters in URL
        invalid_chars = ['\n', '\r', '\t']
        for char in invalid_chars:
            if char in cleaned_base_url:
                raise ValueError(f"Invalid character in base_url: {char!r} found in {base_url!r}")
        
        self.base_url = cleaned_base_url
        # DB증권 API는 tokenP 엔드포인트를 사용할 수도 있음
        self.token_url = f"{self.base_url}/oauth2/tokenP"  # Changed from /oauth2/token to /oauth2/tokenP
        
        # Log cleaned values (without secrets)
        logger.info(f"DB Token Manager initialized with base_url: {self.base_url}")
        logger.info(f"Token URL: {self.token_url}")
        logger.info(f"App Key configured: {'Yes' if self.app_key else 'No'} (length: {len(self.app_key) if self.app_key else 0})")
        logger.info(f"App Secret configured: {'Yes' if self.app_secret else 'No'} (length: {len(self.app_secret) if self.app_secret else 0})")
        
        self.access_token: Optional[str] = None
        self.token_type: str = "Bearer"
        self.expires_at: Optional[datetime] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        
        # Rate limiting and error tracking
        self._last_request_time: Optional[datetime] = None
        self._consecutive_failures = 0
        self._backoff_until: Optional[datetime] = None
        
    async def get_token(self) -> Optional[str]:
        """Get current valid access token"""
        async with self._lock:
            # Check if we're in backoff period
            if self._is_in_backoff():
                logger.warning(f"Token refresh in backoff period until {self._backoff_until}")
                return None
                
            if self._is_token_valid():
                return self.access_token
            
            # Check rate limiting (minimum 30 seconds between requests)
            if self._last_request_time:
                time_since_last = datetime.now(timezone.utc) - self._last_request_time
                if time_since_last.total_seconds() < 30:
                    logger.warning(f"Rate limiting: {30 - time_since_last.total_seconds():.1f}s until next request")
                    return None
            
            # Token expired or not exists, refresh it
            success = await self._refresh_token()
            if success:
                self._consecutive_failures = 0
                self._backoff_until = None
            else:
                self._consecutive_failures += 1
                # Exponential backoff: 1min, 5min, 15min, 30min, 1hr
                backoff_minutes = min(60, 1 * (3 ** self._consecutive_failures))
                self._backoff_until = datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes)
                logger.error(f"Token refresh failed ({self._consecutive_failures} consecutive failures). "
                           f"Next attempt in {backoff_minutes} minutes at {self._backoff_until}")
                
            return self.access_token
    
    def _is_token_valid(self) -> bool:
        """Check if current token is valid (not expired)"""
        if not self.access_token or not self.expires_at:
            return False
        
        # Add 5 minute buffer before expiration
        buffer_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        return self.expires_at > buffer_time
    
    def _is_in_backoff(self) -> bool:
        """Check if we're currently in backoff period"""
        if not self._backoff_until:
            return False
        return datetime.now(timezone.utc) < self._backoff_until
    
    async def _refresh_token(self) -> bool:
        """Refresh access token from DB증권 API"""
        self._last_request_time = datetime.now(timezone.utc)
        
        # Try both token endpoints
        token_endpoints = [
            f"{self.base_url}/oauth2/tokenP",  # DB증권 specific endpoint
            f"{self.base_url}/oauth2/token",   # Standard OAuth2 endpoint
        ]
        
        try:
            # 다양한 토큰 요청 형식 시도 (JSON과 Form-urlencoded 둘 다 시도)
            token_request_formats = [
                # Format 1: Form-urlencoded with charset (한국 증권사 표준)
                {
                    "headers": {
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "User-Agent": "Mozilla/5.0 (compatible; DB-Securities-API-Client)"
                    },
                    "data": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret
                    },
                    "use_form": True
                },
                # Format 2: JSON with charset UTF-8 (일부 증권사 요구)
                {
                    "headers": {
                        "Content-Type": "application/json; charset=UTF-8",
                        "User-Agent": "Mozilla/5.0 (compatible; DB-Securities-API-Client)"
                    },
                    "data": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret
                    },
                    "use_form": False
                },
                # Format 3: JSON format (fallback)
                {
                    "headers": {
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (compatible; DB-Securities-API-Client)"
                    },
                    "data": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret
                    },
                    "use_form": False
                },
                # Format 4: Form-urlencoded without grant_type (일부 API에서 사용)
                {
                    "headers": {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0 (compatible; DB-Securities-API-Client)"
                    },
                    "data": {
                        "appkey": self.app_key,
                        "appsecret": self.app_secret
                    },
                    "use_form": True
                }
            ]
            
            timeout = httpx.Timeout(30.0)  # Increased timeout
            
            for endpoint in token_endpoints:
                logger.info(f"Trying endpoint: {endpoint}")
                
                for i, format_config in enumerate(token_request_formats):
                    try:
                        logger.info(f"Attempting token request format {i+1}/{len(token_request_formats)} on {endpoint}")
                        
                        async with httpx.AsyncClient(timeout=timeout) as client:
                            # Use form-urlencoded or JSON based on format configuration
                            if format_config.get("use_form"):
                                response = await client.post(
                                    endpoint,
                                    headers=format_config["headers"],
                                    data=format_config["data"]  # form-urlencoded
                                )
                            else:
                                response = await client.post(
                                    endpoint,
                                    headers=format_config["headers"],
                                    json=format_config["data"]  # JSON
                                )
                        
                            if response.status_code == 200:
                                token_data = response.json()
                                self.access_token = token_data.get("access_token")
                                expires_in = token_data.get("expires_in", 86400)  # Default 24h
                                self.token_type = token_data.get("token_type", "Bearer")
                                
                                # Set expiration time
                                self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                                
                                # Update the working token URL for future use
                                self.token_url = endpoint
                                
                                logger.info(f"Token refreshed successfully with format {i+1} on {endpoint}, expires at: {self.expires_at}")
                                return True
                        
                        # Handle specific DB증권 error codes
                        elif response.status_code == 403:
                            error_data = {}
                            try:
                                error_data = response.json()
                            except:
                                pass
                                
                            error_code = error_data.get("error_code", "")
                            error_desc = error_data.get("error_description", response.text)
                            
                            # Log specific error for debugging
                            logger.debug(f"Response headers: {dict(response.headers)}")
                            logger.debug(f"Response text: {response.text[:500]}")  # First 500 chars
                            
                            if error_code == "IGW00103" or "Content-Type" in error_desc:
                                logger.error(f"Invalid request format or AppKey (format {i+1} on {endpoint}): {error_desc}")
                                continue  # Try next format
                            elif error_code == "IGW00201":
                                logger.error(f"API call limit exceeded: {error_desc}")
                                logger.error("Stopping token refresh attempts to prevent further quota usage")
                                return False
                            else:
                                logger.error(f"Token refresh failed (format {i+1}): {response.status_code} - {error_desc}")
                                continue
                            else:
                                logger.warning(f"Token request format {i+1} on {endpoint} failed: {response.status_code} - {response.text}")
                                continue
                            
                    except Exception as e:
                        logger.error(f"Token request format {i+1} on {endpoint} error: {e}")
                        continue
                    
                    # Add small delay between format attempts
                    if i < len(token_request_formats) - 1:
                        await asyncio.sleep(0.5)
                
                # Add delay between endpoint attempts
                await asyncio.sleep(1)
            
            logger.error("All token request formats on all endpoints failed")
            return False
                    
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return False
    
    async def start_auto_refresh(self):
        """Start automatic token refresh task (every 23 hours)"""
        if self._refresh_task and not self._refresh_task.done():
            return
        
        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
        logger.info("Auto refresh task started")
    
    async def stop_auto_refresh(self):
        """Stop automatic token refresh task"""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("Auto refresh task stopped")
    
    async def _auto_refresh_loop(self):
        """Auto refresh loop - runs every 23 hours"""
        try:
            # Initial token fetch
            await self.get_token()
            
            while True:
                # Wait 23 hours (23 * 3600 seconds)
                await asyncio.sleep(23 * 3600)
                
                async with self._lock:
                    await self._refresh_token()
                    
        except asyncio.CancelledError:
            logger.info("Auto refresh loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Auto refresh loop error: {e}")
    
    def get_auth_header(self) -> Dict[str, str]:
        """Get authorization header for API requests"""
        if not self.access_token:
            return {}
        return {"Authorization": f"{self.token_type} {self.access_token}"}
    
    async def health_check(self) -> Dict[str, Any]:
        """Check token manager health status"""
        return {
            "token_valid": self._is_token_valid(),
            "has_token": bool(self.access_token),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_task_active": bool(self._refresh_task and not self._refresh_task.done()),
            "consecutive_failures": self._consecutive_failures,
            "in_backoff": self._is_in_backoff(),
            "backoff_until": self._backoff_until.isoformat() if self._backoff_until else None,
            "last_request_time": self._last_request_time.isoformat() if self._last_request_time else None
        }


# Global token manager instance
_token_manager: Optional[DBSecTokenManager] = None


def get_token_manager() -> Optional[DBSecTokenManager]:
    """Get global token manager instance"""
    global _token_manager
    
    if _token_manager is None:
        # Strip whitespace and newlines from environment variables
        app_key = os.getenv("DB_APP_KEY", "").strip()
        app_secret = os.getenv("DB_APP_SECRET", "").strip()
        base_url = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443").strip()
        
        if not app_key or not app_secret:
            logger.warning("DB_APP_KEY or DB_APP_SECRET not configured")
            return None
        
        try:
            _token_manager = DBSecTokenManager(app_key, app_secret, base_url)
        except ValueError as e:
            logger.error(f"Failed to initialize token manager: {e}")
            return None
    
    return _token_manager


async def init_token_manager():
    """Initialize and start the global token manager"""
    manager = get_token_manager()
    if manager:
        await manager.start_auto_refresh()
        logger.info("DB Token Manager initialized and started")


async def shutdown_token_manager():
    """Shutdown the global token manager"""
    global _token_manager
    if _token_manager:
        await _token_manager.stop_auto_refresh()
        _token_manager = None
        logger.info("DB Token Manager shutdown")