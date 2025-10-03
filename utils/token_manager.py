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
            logger.info(f"DB Token Manager ENABLED - will connect to: {self.base_url}")
            logger.info(f"Token URL: {self.token_url}")
            logger.info(f"App Key configured: {'Yes' if self.app_key else 'No'} (length: {len(self.app_key) if self.app_key else 0})")
            logger.info(f"App Secret configured: {'Yes' if self.app_secret else 'No'} (length: {len(self.app_secret) if self.app_secret else 0})")
            
            self.access_token: Optional[str] = None
            self.token_type: str = "Bearer"
            self.expires_at: Optional[datetime] = None
        else:
            logger.warning("DB Token Manager DISABLED (mock mode) - no API calls will be made")
            # Use mock token for disabled mode
            self.access_token: Optional[str] = "MOCK_TOKEN_DISABLED"
            self.token_type: str = "Bearer"
            self.expires_at: Optional[datetime] = datetime.now(timezone.utc) + timedelta(days=365)
        
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        
        # Rate limiting and error tracking (only for enabled mode)
        self._last_request_time: Optional[datetime] = None
        self._consecutive_failures = 0
        self._backoff_until: Optional[datetime] = None
        
    async def get_token(self) -> Optional[str]:
        """Get current valid access token"""
        if not self.enabled:
            return self.access_token  # Return mock token
            
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
        if not self.enabled:
            return True  # Mock token is always valid
            
        if not self.access_token or not self.expires_at:
            return False
        
        # Add 5 minute buffer before expiration
        buffer_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        return self.expires_at > buffer_time
    
    def _is_in_backoff(self) -> bool:
        """Check if we're currently in backoff period"""
        if not self.enabled:
            return False  # No backoff in mock mode
            
        if not self._backoff_until:
            return False
        return datetime.now(timezone.utc) < self._backoff_until
    
    async def _refresh_token(self) -> bool:
        """Refresh access token from DB증권 API"""
        if not self.enabled:
            logger.debug("Token refresh skipped - mock mode")
            return True
            
        self._last_request_time = datetime.now(timezone.utc)
        
        try:
            # DB증권 API requires specific format based on Korean securities API patterns
            token_request_formats = [
                # Format 1: Standard form-urlencoded (most Korean brokers)
                {
                    "headers": {
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    "data": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret
                    },
                    "use_form": True
                },
                # Format 2: With scope parameter
                {
                    "headers": {
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    "data": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret,
                        "scope": "oob"
                    },
                    "use_form": True
                },
                # Format 3: JSON format as fallback
                {
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "data": {
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret
                    },
                    "use_form": False
                }
            ]
            
            timeout = httpx.Timeout(30.0)
            
            for i, format_config in enumerate(token_request_formats):
                try:
                    logger.info(f"Attempting token request format {i+1}/{len(token_request_formats)}")
                    logger.debug(f"Request headers: {format_config['headers']}")
                    logger.debug(f"Request data fields: {list(format_config['data'].keys())}")
                    
                    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                        # Use form-urlencoded or JSON based on format configuration
                        if format_config.get("use_form"):
                            response = await client.post(
                                self.token_url,
                                headers=format_config["headers"],
                                data=format_config["data"]  # form-urlencoded
                            )
                        else:
                            response = await client.post(
                                self.token_url,
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
                            
                            logger.info(f"Token refreshed successfully with format {i+1}, expires at: {self.expires_at}")
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
                                logger.error(f"Invalid request format or AppKey (format {i+1}): {error_desc}")
                                continue  # Try next format
                            elif error_code == "IGW00105":
                                logger.error(f"Invalid AppSecret: {error_desc}")
                                logger.error("Please check DB_APP_SECRET environment variable")
                                return False  # Don't retry with wrong credentials
                            elif error_code == "IGW00201":
                                logger.error(f"API call limit exceeded: {error_desc}")
                                logger.error("Stopping token refresh attempts to prevent further quota usage")
                                return False
                            else:
                                logger.error(f"Token refresh failed (format {i+1}): {response.status_code} - {error_desc}")
                                continue
                        else:
                            logger.warning(f"Token request format {i+1} failed: {response.status_code} - {response.text}")
                            continue
                            
                except Exception as e:
                    logger.error(f"Token request format {i+1} error: {e}")
                    continue
                
                # Add small delay between format attempts
                if i < len(token_request_formats) - 1:
                    await asyncio.sleep(1)
            
            logger.error("All token request formats failed")
            return False
                    
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return False
    
    async def start_auto_refresh(self):
        """Start automatic token refresh task (every 23 hours)"""
        if not self.enabled:
            logger.info("Auto refresh disabled - mock mode")
            return
            
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
        if not self.enabled:
            return
            
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
        
        try:
            _token_manager = DBSecTokenManager(
                app_key=app_key or "MOCK_KEY",
                app_secret=app_secret or "MOCK_SECRET",
                base_url=base_url,
                enabled=dbsec_enabled
            )
        except ValueError as e:
            logger.error(f"Failed to initialize token manager: {e}")
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