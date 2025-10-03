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
        self.token_url = f"{self.base_url}/oauth2/token"
        
        # Log cleaned values (without secrets)
        logger.info(f"DB Token Manager initialized with base_url: {self.base_url}")
        logger.info(f"Token URL: {self.token_url}")
        
        self.access_token: Optional[str] = None
        self.token_type: str = "Bearer"
        self.expires_at: Optional[datetime] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        
    async def get_token(self) -> Optional[str]:
        """Get current valid access token"""
        async with self._lock:
            if self._is_token_valid():
                return self.access_token
            
            # Token expired or not exists, refresh it
            await self._refresh_token()
            return self.access_token
    
    def _is_token_valid(self) -> bool:
        """Check if current token is valid (not expired)"""
        if not self.access_token or not self.expires_at:
            return False
        
        # Add 5 minute buffer before expiration
        buffer_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        return self.expires_at > buffer_time
    
    async def _refresh_token(self) -> bool:
        """Refresh access token from DB증권 API"""
        try:
            headers = {
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            data = {
                "grant_type": "client_credentials",
                "client_id": self.app_key,
                "client_secret": self.app_secret
            }
            
            timeout = httpx.Timeout(10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.token_url,
                    headers=headers,
                    data=data
                )
                
                if response.status_code == 200:
                    token_data = response.json()
                    self.access_token = token_data.get("access_token")
                    expires_in = token_data.get("expires_in", 86400)  # Default 24h
                    self.token_type = token_data.get("token_type", "Bearer")
                    
                    # Set expiration time
                    self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                    
                    logger.info(f"Token refreshed successfully, expires at: {self.expires_at}")
                    return True
                else:
                    logger.error(f"Token refresh failed: {response.status_code} - {response.text}")
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
            "refresh_task_active": bool(self._refresh_task and not self._refresh_task.done())
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