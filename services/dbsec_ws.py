"""
DB증권 WebSocket Client for KOSPI200 Futures
Handles real-time market data streaming and anomaly detection
"""
import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable, Deque
import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError
import httpx

from utils.token_manager import get_token_manager

logger = logging.getLogger(__name__)


class KOSPI200FuturesMonitor:
    """KOSPI200 Futures real-time monitor with anomaly detection"""
    
    def __init__(
        self,
        alert_threshold: float = 1.0,  # 1% change threshold
        buffer_size: int = 100,
        ws_url: str = "wss://openapi.dbsec.co.kr:9443/ws"
    ):
        # Strip whitespace and validate WebSocket URL
        cleaned_ws_url = ws_url.strip() if ws_url else ""
        if cleaned_ws_url and not cleaned_ws_url.startswith(("ws://", "wss://")):
            raise ValueError(f"Invalid WebSocket URL format: {ws_url!r}. Must start with ws:// or wss://")
        
        # Check for invalid characters in URL
        invalid_chars = ['\n', '\r', '\t']
        for char in invalid_chars:
            if char in cleaned_ws_url:
                raise ValueError(f"Invalid character in WebSocket URL: {char!r} found in {ws_url!r}")
        
        self.ws_url = cleaned_ws_url
        self.alert_threshold = alert_threshold
        self.buffer_size = buffer_size
        
        logger.info(f"KOSPI200 Monitor initialized with WebSocket URL: {self.ws_url}")
        
        # Data buffers
        self.tick_buffer: Deque[Dict[str, Any]] = deque(maxlen=buffer_size)
        self.last_price: Optional[float] = None
        self.last_alert_time: Optional[datetime] = None
        
        # Connection state
        self.websocket: Optional[websockets.WebSocketServerProtocol] = None
        self.is_connected: bool = False
        self.reconnect_attempts: int = 0
        self.max_reconnect_attempts: int = 10
        
        # Event handlers
        self.alert_callback: Optional[Callable] = None
        
        # Session tracking
        self.current_session: str = "UNKNOWN"  # DAY or NIGHT
        
        # Caia Agent configuration
        self.caia_agent_url = os.getenv("CAIA_AGENT_URL", "").strip()
        
    def set_alert_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback function for alert notifications"""
        self.alert_callback = callback
        
    async def start_monitoring(self):
        """Start WebSocket monitoring with auto-reconnect"""
        logger.info("Starting KOSPI200 Futures monitoring")
        
        while self.reconnect_attempts < self.max_reconnect_attempts:
            try:
                await self._connect_and_monitor()
                
            except (ConnectionClosed, ConnectionClosedError) as e:
                self.reconnect_attempts += 1
                logger.warning(f"WebSocket connection lost (attempt {self.reconnect_attempts}): {e}")
                
                if self.reconnect_attempts < self.max_reconnect_attempts:
                    # Exponential backoff: 2^attempt seconds (max 60s)
                    backoff = min(2 ** self.reconnect_attempts, 60)
                    logger.info(f"Reconnecting in {backoff} seconds...")
                    await asyncio.sleep(backoff)
                else:
                    logger.error("Max reconnection attempts exceeded")
                    break
                    
            except Exception as e:
                logger.error(f"Monitoring error: {e}")
                self.reconnect_attempts += 1
                await asyncio.sleep(5)
        
        logger.error("WebSocket monitoring stopped")
        
    async def _connect_and_monitor(self):
        """Connect to WebSocket and start monitoring"""
        token_manager = get_token_manager()
        if not token_manager:
            raise Exception("Token manager not available")
        
        # Get access token
        access_token = await token_manager.get_token()
        if not access_token:
            raise Exception("Failed to get access token")
        
        headers = {
            "Authorization": f"Bearer {access_token}"
        }
        
        logger.info(f"Connecting to WebSocket: {self.ws_url}")
        
        async with websockets.connect(
            self.ws_url,
            extra_headers=headers,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=10
        ) as websocket:
            self.websocket = websocket
            self.is_connected = True
            self.reconnect_attempts = 0
            
            logger.info("WebSocket connected successfully")
            
            # Subscribe to KOSPI200 futures
            await self._subscribe_futures()
            
            # Listen for messages
            async for message in websocket:
                await self._handle_message(message)
                
    async def _subscribe_futures(self):
        """Subscribe to KOSPI200 futures real-time data"""
        # DB증권 API specific subscription message
        # This is a placeholder - actual format depends on DB증권 API specification
        subscribe_msg = {
            "header": {
                "tr_type": "1",  # 실시간 등록
                "tr_key": "K200_FUT"  # KOSPI200 선물
            },
            "body": {
                "rt_cd": "S3_",  # 선물 실시간 코드 (예시)
                "ivno": "101P3000"  # KOSPI200 선물 종목번호 (예시)
            }
        }
        
        await self.websocket.send(json.dumps(subscribe_msg))
        logger.info("Subscribed to KOSPI200 futures real-time data")
        
    async def _handle_message(self, message: str):
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)
            
            # Parse futures data (format depends on DB증권 API)
            tick_data = await self._parse_tick_data(data)
            if not tick_data:
                return
                
            # Add to buffer
            self.tick_buffer.append(tick_data)
            
            # Check for anomalies
            await self._check_anomaly(tick_data)
            
        except Exception as e:
            logger.error(f"Message handling error: {e}")
            
    async def _parse_tick_data(self, raw_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse raw WebSocket data into standardized tick format"""
        try:
            # This is a placeholder parsing logic
            # Actual implementation depends on DB증권 API response format
            
            # Example parsing (to be updated based on actual API spec):
            body = raw_data.get("body", {})
            
            current_price = float(body.get("stck_prpr", 0))  # 현재가
            if current_price <= 0:
                return None
                
            # Determine trading session
            session = self._determine_session()
            
            tick = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": "K200_FUT",
                "price": current_price,
                "volume": int(body.get("cntg_vol", 0)),  # 체결량
                "session": session,
                "change_rate": 0.0,  # Will be calculated below
                "raw_data": raw_data
            }
            
            # Calculate change rate if we have previous price
            if self.last_price and self.last_price > 0:
                change_rate = ((current_price - self.last_price) / self.last_price) * 100
                tick["change_rate"] = change_rate
                
            self.last_price = current_price
            self.current_session = session
            
            return tick
            
        except Exception as e:
            logger.error(f"Tick parsing error: {e}")
            return None
            
    def _determine_session(self) -> str:
        """Determine if current time is DAY or NIGHT session"""
        from datetime import datetime, time
        import pytz
        
        # Korean timezone
        kst = pytz.timezone('Asia/Seoul')
        now = datetime.now(kst).time()
        
        # KOSPI200 futures trading hours (KST)
        # Day session: 09:00 - 15:15
        # Night session: 18:00 - 05:00 (next day)
        
        day_start = time(9, 0)   # 09:00
        day_end = time(15, 15)   # 15:15
        night_start = time(18, 0) # 18:00
        night_end = time(5, 0)    # 05:00 (next day)
        
        if day_start <= now <= day_end:
            return "DAY"
        elif now >= night_start or now <= night_end:
            return "NIGHT"
        else:
            return "UNKNOWN"
            
    async def _check_anomaly(self, tick: Dict[str, Any]):
        """Check for price anomalies and trigger alerts"""
        change_rate = abs(tick.get("change_rate", 0))
        
        if change_rate >= self.alert_threshold:
            # Avoid spam: don't alert more than once per minute for same condition
            now = datetime.now(timezone.utc)
            if (self.last_alert_time and 
                (now - self.last_alert_time).total_seconds() < 60):
                return
                
            self.last_alert_time = now
            
            # Create alert payload
            alert_payload = {
                "symbol": tick["symbol"],
                "session": tick["session"],
                "change": tick["change_rate"],
                "price": tick["price"],
                "timestamp": tick["timestamp"],
                "threshold": self.alert_threshold,
                "alert_type": "price_anomaly"
            }
            
            logger.warning(f"ANOMALY DETECTED: {tick['symbol']} {tick['change_rate']:.2f}% change in {tick['session']} session")
            
            # Send to Caia Agent
            await self._send_to_caia_agent(alert_payload)
            
            # Call custom callback if set
            if self.alert_callback:
                try:
                    self.alert_callback(alert_payload)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")
                    
    async def _send_to_caia_agent(self, payload: Dict[str, Any]):
        """Send alert to Caia Agent /report endpoint"""
        if not self.caia_agent_url:
            logger.warning("CAIA_AGENT_URL not configured, skipping agent notification")
            return
            
        try:
            report_url = f"{self.caia_agent_url.rstrip('/')}/report"
            
            timeout = httpx.Timeout(10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    report_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )
                
                if response.status_code == 200:
                    logger.info(f"Alert sent to Caia Agent: {response.status_code}")
                else:
                    logger.error(f"Caia Agent error: {response.status_code} - {response.text}")
                    
        except Exception as e:
            logger.error(f"Failed to send alert to Caia Agent: {e}")
            
    def get_recent_ticks(self, limit: Optional[int] = None) -> list:
        """Get recent tick data from buffer"""
        if limit is None:
            limit = len(self.tick_buffer)
        return list(self.tick_buffer)[-limit:]
        
    def get_health_status(self) -> Dict[str, Any]:
        """Get WebSocket connection and monitoring health status"""
        return {
            "connected": self.is_connected,
            "reconnect_attempts": self.reconnect_attempts,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "buffer_size": len(self.tick_buffer),
            "max_buffer_size": self.buffer_size,
            "last_price": self.last_price,
            "current_session": self.current_session,
            "alert_threshold": self.alert_threshold,
            "last_alert_time": self.last_alert_time.isoformat() if self.last_alert_time else None
        }
        
    async def stop_monitoring(self):
        """Stop WebSocket monitoring"""
        self.is_connected = False
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        logger.info("WebSocket monitoring stopped")


# Global monitor instance
_futures_monitor: Optional[KOSPI200FuturesMonitor] = None


def get_futures_monitor() -> KOSPI200FuturesMonitor:
    """Get global KOSPI200 futures monitor instance"""
    global _futures_monitor
    
    if _futures_monitor is None:
        try:
            alert_threshold = float(os.getenv("DB_ALERT_THRESHOLD", "1.0").strip())
            buffer_size = int(os.getenv("DB_BUFFER_SIZE", "100").strip())
            ws_url = os.getenv("DB_WS_URL", "wss://openapi.dbsec.co.kr:9443/ws").strip()
            
            _futures_monitor = KOSPI200FuturesMonitor(
                alert_threshold=alert_threshold,
                buffer_size=buffer_size,
                ws_url=ws_url
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to initialize futures monitor: {e}")
            # Create a default monitor as fallback
            _futures_monitor = KOSPI200FuturesMonitor()
    
    return _futures_monitor


async def start_futures_monitoring():
    """Start the global futures monitor"""
    monitor = get_futures_monitor()
    # Start monitoring in background task
    asyncio.create_task(monitor.start_monitoring())
    logger.info("KOSPI200 Futures monitoring started")


async def stop_futures_monitoring():
    """Stop the global futures monitor"""
    global _futures_monitor
    if _futures_monitor:
        await _futures_monitor.stop_monitoring()
        _futures_monitor = None
    logger.info("KOSPI200 Futures monitoring stopped")