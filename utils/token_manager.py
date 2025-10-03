"""
DB증권 Token Manager - DISABLED VERSION
Token acquisition is disabled to prevent API quota exhaustion
"""
import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class DBSecTokenManager:
    """DB증권 API token manager - DISABLED to prevent quota issues"""
    
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        base_url: str = "https://openapi.dbsec.co.kr:8443"
    ):
        self.app_key = app_key.strip() if app_key else ""
        self.app_secret = app_secret.strip() if app_secret else ""
        self.base_url = base_url.strip().rstrip("/") if base_url else ""
        
        # DISABLED - Using mock token to prevent API calls
        self.access_token: Optional[str] = "MOCK_TOKEN_DISABLED"
        self.token_type: str = "Bearer"
        self.expires_at: Optional[datetime] = datetime.now(timezone.utc) + timedelta(days=365)
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        
        logger.warning("DB Token Manager is DISABLED - using mock token to prevent API quota issues")
        
    async def get_token(self) -> Optional[str]:
        """Return mock token - actual API is disabled"""
        return self.access_token
    
    def _is_token_valid(self) -> bool:
        """Always return True for mock token"""
        return True
    
    def _is_in_backoff(self) -> bool:
        """No backoff needed for mock"""
        return False
    
    async def _refresh_token(self) -> bool:
        """Skip token refresh - using mock"""
        logger.info("Token refresh skipped - using mock token")
        return True
    
    async def start_auto_refresh(self):
        """No auto refresh needed for mock token"""
        logger.info("Auto refresh disabled - using mock token")
        return
    
    async def stop_auto_refresh(self):
        """No auto refresh to stop"""
        return
    
    async def _auto_refresh_loop(self):
        """Disabled auto refresh loop"""
        return
    
    def get_auth_header(self) -> Dict[str, str]:
        """Get mock authorization header"""
        return {"Authorization": f"{self.token_type} {self.access_token}"}
    
    async def health_check(self) -> Dict[str, Any]:
        """Check token manager health status"""
        return {
            "token_valid": True,
            "has_token": True,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_task_active": False,
            "consecutive_failures": 0,
            "in_backoff": False,
            "backoff_until": None,
            "last_request_time": None,
            "status": "DISABLED - Using mock token to prevent API quota issues"
        }


# Global token manager instance
_token_manager: Optional[DBSecTokenManager] = None


def get_token_manager() -> Optional[DBSecTokenManager]:
    """Get global token manager instance"""
    global _token_manager
    
    if _token_manager is None:
        app_key = os.getenv("DB_APP_KEY", "").strip()
        app_secret = os.getenv("DB_APP_SECRET", "").strip()
        base_url = os.getenv("DB_API_BASE", "https://openapi.dbsec.co.kr:8443").strip()
        
        # Create disabled token manager even without credentials
        _token_manager = DBSecTokenManager(
            app_key or "DISABLED",
            app_secret or "DISABLED",
            base_url
        )
    
    return _token_manager


async def init_token_manager():
    """Initialize and start the global token manager"""
    manager = get_token_manager()
    if manager:
        await manager.start_auto_refresh()
        logger.warning("DB Token Manager initialized in DISABLED mode - no API calls will be made")


async def shutdown_token_manager():
    """Shutdown the global token manager"""
    global _token_manager
    if _token_manager:
        await _token_manager.stop_auto_refresh()
        _token_manager = None
        logger.info("DB Token Manager shutdown")