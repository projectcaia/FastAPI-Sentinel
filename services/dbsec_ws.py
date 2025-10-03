"""
DB증권 WebSocket Client for KOSPI200 Futures - DISABLED VERSION
WebSocket monitoring is disabled to prevent API quota issues
"""
import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable, Deque

from utils.token_manager import get_token_manager

logger = logging.getLogger(__name__)


class KOSPI200FuturesMonitor:
    """KOSPI200 Futures monitor - DISABLED to prevent API issues"""
    
    def __init__(
        self,
        alert_threshold: float = 1.0,
        warn_threshold: float = 0.5,
        buffer_size: int = 100,
        ws_url: str = "wss://openapi.dbsec.co.kr:9443/ws"
    ):
        self.ws_url = ws_url
        self.alert_threshold = alert_threshold
        self.warn_threshold = warn_threshold
        self.buffer_size = buffer_size
        
        logger.warning("KOSPI200 Monitor is DISABLED - no WebSocket connection will be made")
        
        # Data buffers
        self.tick_buffer: Deque[Dict[str, Any]] = deque(maxlen=buffer_size)
        self.last_price: Optional[float] = None
        self.last_alert_time: Optional[datetime] = None
        self.daily_open_price: Optional[float] = None
        
        # Connection state
        self.websocket = None
        self.is_connected: bool = False
        self.reconnect_attempts: int = 0
        self.max_reconnect_attempts: int = 0  # Set to 0 to disable
        
        # Event handlers
        self.alert_callback: Optional[Callable] = None
        
        # Session tracking
        self.current_session: str = "DISABLED"
        
        # MarketWatcher integration
        self.sentinel_base_url = os.getenv("SENTINEL_BASE_URL", "").strip()
        self.sentinel_key = os.getenv("SENTINEL_KEY", "").strip()
        
    def set_alert_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback function for alert notifications"""
        self.alert_callback = callback
        
    async def start_monitoring(self):
        """Skip WebSocket monitoring - service is disabled"""
        logger.warning("KOSPI200 Futures monitoring is DISABLED - no WebSocket connection will be made")
        # Sleep indefinitely to keep the task alive but do nothing
        while True:
            await asyncio.sleep(3600)  # Sleep for 1 hour
        
    async def _connect_and_monitor(self):
        """Skip connection - service is disabled"""
        return
        
    async def _subscribe_futures(self):
        """Skip subscription - service is disabled"""
        return
        
    async def _handle_message(self, message: str):
        """Skip message handling - service is disabled"""
        return
            
    async def _parse_tick_data(self, raw_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Skip parsing - service is disabled"""
        return None
            
    def _determine_session(self) -> str:
        """Return DISABLED status"""
        return "DISABLED"
            
    async def _check_anomaly(self, tick: Dict[str, Any]):
        """Skip anomaly check - service is disabled"""
        return
                    
    async def _send_to_market_watcher(self, payload: Dict[str, Any]):
        """Skip alert sending - service is disabled"""
        return
    
    def _grade_level(self, change_pct: float) -> str:
        """Grade alert level based on change percentage"""
        abs_change = abs(change_pct)
        if abs_change >= 2.5:
            return "LV3"
        elif abs_change >= 1.5:
            return "LV2"
        elif abs_change >= 0.8:
            return "LV1"
        return None
            
    def get_recent_ticks(self, limit: Optional[int] = None) -> list:
        """Return empty list - service is disabled"""
        return []
        
    def get_health_status(self) -> Dict[str, Any]:
        """Get health status - showing disabled state"""
        return {
            "connected": False,
            "reconnect_attempts": 0,
            "max_reconnect_attempts": 0,
            "buffer_size": 0,
            "max_buffer_size": self.buffer_size,
            "last_price": None,
            "current_session": "DISABLED",
            "alert_threshold": self.alert_threshold,
            "warn_threshold": self.warn_threshold,
            "last_alert_time": None,
            "status": "DISABLED - WebSocket monitoring is turned off to prevent API quota issues"
        }
        
    async def stop_monitoring(self):
        """Stop monitoring - already disabled"""
        logger.info("WebSocket monitoring stop called (already disabled)")


# Global monitor instance
_futures_monitor: Optional[KOSPI200FuturesMonitor] = None


def get_futures_monitor() -> KOSPI200FuturesMonitor:
    """Get global KOSPI200 futures monitor instance"""
    global _futures_monitor
    
    if _futures_monitor is None:
        try:
            alert_threshold = float(os.getenv("DB_ALERT_THRESHOLD", "1.0").strip())
            warn_threshold = float(os.getenv("DB_WARN_THRESHOLD", "0.5").strip())
            buffer_size = int(os.getenv("DB_BUFFER_SIZE", "100").strip())
            ws_url = os.getenv("DB_WS_URL", "wss://openapi.dbsec.co.kr:9443/ws").strip()
            
            _futures_monitor = KOSPI200FuturesMonitor(
                alert_threshold=alert_threshold,
                warn_threshold=warn_threshold,
                buffer_size=buffer_size,
                ws_url=ws_url
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to initialize futures monitor: {e}")
            _futures_monitor = KOSPI200FuturesMonitor()
    
    return _futures_monitor


async def start_futures_monitoring():
    """Start the global futures monitor - DISABLED"""
    monitor = get_futures_monitor()
    # Start monitoring in background task (will just sleep)
    asyncio.create_task(monitor.start_monitoring())
    logger.warning("KOSPI200 Futures monitoring started in DISABLED mode")


async def stop_futures_monitoring():
    """Stop the global futures monitor"""
    global _futures_monitor
    if _futures_monitor:
        await _futures_monitor.stop_monitoring()
        _futures_monitor = None
    logger.info("KOSPI200 Futures monitoring stopped")