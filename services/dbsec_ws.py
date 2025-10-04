"""
DB증권 WebSocket Client for KOSPI200 Futures
Handles real-time market data streaming and anomaly detection
"""
import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone, time, timedelta
from typing import Optional, Dict, Any, Callable, Deque
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import websocket
from websocket import WebSocketException
from websocket._exceptions import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)
import requests

from utils.masking import mask_secret, redact_headers, redact_ws_url, redact_dict
from utils.token_manager import get_token_manager
from app.utils import is_krx_trading_day

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
DAY_SESSION_START = time(9, 0)
DAY_SESSION_END = time(15, 45)
NIGHT_SESSION_START = time(18, 0)
NIGHT_SESSION_END = time(5, 0)


def _ensure_kst(now: Optional[datetime] = None) -> datetime:
    """Normalize input datetime to Asia/Seoul timezone."""
    if now is None:
        return datetime.now(KST)

    if now.tzinfo is None:
        return now.replace(tzinfo=KST)

    return now.astimezone(KST)


def determine_trading_session(now: Optional[datetime] = None) -> str:
    """Return the current trading session label (DAY/NIGHT/CLOSED)."""
    now_kst = _ensure_kst(now)
    current_time = now_kst.time()

    if DAY_SESSION_START <= current_time <= DAY_SESSION_END:
        return "DAY" if is_krx_trading_day(now_kst.date()) else "CLOSED"

    if current_time >= NIGHT_SESSION_START or current_time <= NIGHT_SESSION_END:
        reference_date = now_kst.date()
        if current_time <= NIGHT_SESSION_END:
            reference_date = reference_date - timedelta(days=1)

        return "NIGHT" if is_krx_trading_day(reference_date) else "CLOSED"

    return "CLOSED"


def is_trading_session(now: Optional[datetime] = None) -> bool:
    """Return True when KOSPI200 futures trading is active."""
    return determine_trading_session(now) in {"DAY", "NIGHT"}
class KOSPI200FuturesMonitor:
    """KOSPI200 Futures real-time monitor with anomaly detection"""
    
    def __init__(
        self,
        alert_threshold: float = 1.0,  # 1% change threshold for CRITICAL
        warn_threshold: float = 0.5,   # 0.5% change threshold for WARN
        buffer_size: int = 100,
        ws_url: str = "wss://openapi.dbsec.co.kr:9443/ws",
        enabled: bool = True
    ):
        self.enabled = enabled
        
        # Strip whitespace and validate WebSocket URL
        cleaned_ws_url = ws_url.strip() if ws_url else ""
        if enabled and cleaned_ws_url and not cleaned_ws_url.startswith(("ws://", "wss://")):
            raise ValueError(f"Invalid WebSocket URL format: {ws_url!r}. Must start with ws:// or wss://")
        
        self.ws_url = cleaned_ws_url
        self.alert_threshold = alert_threshold  # For CRITICAL level
        self.warn_threshold = warn_threshold    # For WARN level
        self.buffer_size = buffer_size
        
        if self.enabled:
            logger.info(
                "[DB증권] K200 Futures Monitor ENABLED - WebSocket URL: %s",
                redact_ws_url(self.ws_url),
            )
        else:
            logger.warning("[DB증권] K200 Futures Monitor DISABLED (mock mode) - no WebSocket connection")
        
        # Data buffers
        self.tick_buffer: Deque[Dict[str, Any]] = deque(maxlen=buffer_size)
        self.last_price: Optional[float] = None
        self.last_alert_time: Optional[datetime] = None
        self.daily_open_price: Optional[float] = None  # Store daily open for % calculation
        
        # Connection state
        self.websocket: Optional[websocket.WebSocket] = None
        self.is_connected: bool = False
        self.reconnect_attempts: int = 0
        self.max_reconnect_attempts: Optional[int] = None
        
        # Event handlers
        self.alert_callback: Optional[Callable] = None
        
        # Session tracking
        self.current_session: str = "UNKNOWN"  # DAY or NIGHT
        
        # MarketWatcher integration endpoint (uses main Sentinel URL)
        self.sentinel_base_url = os.getenv("SENTINEL_BASE_URL", "").strip()
        self.sentinel_key = os.getenv("SENTINEL_KEY", "").strip()
        
    def set_alert_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback function for alert notifications"""
        self.alert_callback = callback
        
    async def start_monitoring(self):
        """Start WebSocket monitoring with auto-reconnect"""
        if not self.enabled:
            logger.info("[DB증권] K200 Futures monitoring skipped - mock mode")
            # Keep task alive but do nothing
            while True:
                await asyncio.sleep(3600)  # Sleep for 1 hour
            return
            
        logger.info("[DB증권] Starting K200 Futures monitoring")
        
        while True:
            if not is_trading_session():
                logger.info("[DBSEC] 휴장일/비거래 시간 → WebSocket 연결 skip (대기)")
                self.is_connected = False
                self.reconnect_attempts = 0
                await asyncio.sleep(60)
                continue

            try:
                await self._connect_and_monitor()

            except asyncio.TimeoutError:
                logger.warning("[DBSEC] WebSocket timeout, retrying...")
                self.is_connected = False
                await asyncio.sleep(5)
                continue

            except (WebSocketConnectionClosedException, WebSocketException) as e:
                self.reconnect_attempts += 1
                logger.warning(f"WebSocket connection lost (attempt {self.reconnect_attempts}): {e}")

                backoff = min(2 ** self.reconnect_attempts, 60)
                logger.info(f"Reconnecting in {backoff} seconds...")
                await asyncio.sleep(backoff)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Monitoring error: {error_msg}")

                # Handle token-related errors with longer backoff
                if "token" in error_msg.lower() or "access" in error_msg.lower():
                    self.reconnect_attempts += 1
                    # Longer backoff for token issues: 60s, 120s, 300s, etc.
                    backoff = min(60 * (2 ** (self.reconnect_attempts - 1)), 1800)  # Max 30min
                    logger.warning(f"Token-related error, waiting {backoff}s before retry...")
                    await asyncio.sleep(backoff)
                else:
                    self.reconnect_attempts += 1
                    await asyncio.sleep(5)
        
    async def _connect_and_monitor(self):
        """Connect to WebSocket and start monitoring"""
        token_manager = get_token_manager()
        if not token_manager:
            raise Exception("Token manager not available")
        
        # Get access token
        access_token = await token_manager.get_token()
        if not access_token:
            # Check if we're in backoff
            if hasattr(token_manager, '_is_in_backoff') and token_manager._is_in_backoff():
                raise Exception(f"Token manager in backoff period until {token_manager._backoff_until}")
            else:
                raise Exception("Failed to get access token - check DB_APP_KEY/DB_APP_SECRET or API quota")
        
        app_key = getattr(token_manager, "app_key", "").strip()
        if not app_key:
            raise Exception("DB_APP_KEY is not configured - check environment variables")

        parsed_url = urlparse(self.ws_url)
        query_params = dict(parse_qsl(parsed_url.query))
        query_params.update({
            "appkey": app_key,
            "token": access_token,
        })
        ws_url = urlunparse(parsed_url._replace(query=urlencode(query_params)))

        logger.info("Connecting to WebSocket: %s", redact_ws_url(ws_url))

        headers: Dict[str, str] = {}
        send_auth_header = os.getenv("DBSEC_WS_SEND_AUTH_HEADER", "false").lower() in ("1", "true", "yes")
        if send_auth_header:
            token_type = getattr(token_manager, "token_type", "Bearer")
            headers["Authorization"] = f"{token_type} {access_token}"
            logger.debug(
                "Including Authorization header for WebSocket handshake: %s",
                redact_headers(headers),
            )

        header_list = [f"{k}: {v}" for k, v in headers.items()]

        ws = await asyncio.to_thread(
            websocket.create_connection,
            ws_url,
            header=header_list,
            timeout=30,
            enable_multithread=True,
        )

        self.websocket = ws
        self.is_connected = True
        self.reconnect_attempts = 0

        logger.info("WebSocket connected successfully")

        # Ensure recv does not block forever
        await asyncio.to_thread(ws.settimeout, 30)

        try:
            # Subscribe to KOSPI200 futures
            await self._subscribe_futures()

            # Listen for messages
            while self.is_connected:
                try:
                    message = await asyncio.to_thread(ws.recv)
                except WebSocketTimeoutException:
                    logger.warning("[DBSEC] WebSocket timeout, retrying...")
                    await asyncio.sleep(5)
                    continue

                if message is None:
                    continue

                if isinstance(message, bytes):
                    message = message.decode("utf-8", "ignore")

                await self._handle_message(message)

        except WebSocketConnectionClosedException as exc:
            raise exc
        finally:
            self.is_connected = False
            if self.websocket:
                try:
                    await asyncio.to_thread(self.websocket.close)
                finally:
                    self.websocket = None
                
    async def _subscribe_futures(self):
        """Subscribe to KOSPI200 futures real-time data"""
        # DB증권 API 실시간 선물 구독 명세 기반 메시지 구성
        symbol = os.getenv("DB_FUTURES_SYMBOL", "K200").strip() or "K200"
        exchange = os.getenv("DB_FUTURES_EXCHANGE", "FUT").strip() or "FUT"
        tr_id = os.getenv("DB_FUTURES_TR_ID", "HDFSCNT0").strip() or "HDFSCNT0"

        subscribe_msg = {
            "header": {
                "tr_type": "1",          # 1: 실시간 등록, 2: 실시간 해제
                "content_type": "utf-8",  # 명세에 따른 인코딩 정보
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "symbol": symbol,
                    "exchange": exchange,
                }
            }
        }

        if not self.websocket:
            raise RuntimeError("WebSocket connection is not established")

        await asyncio.to_thread(self.websocket.send, json.dumps(subscribe_msg))
        logger.info("[DBSEC] Sent subscribe_msg for K200 Futures")
        
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

            tick_summary = {
                "price": tick_data.get("price"),
                "change_rate": tick_data.get("change_rate"),
                "volume": tick_data.get("volume"),
                "session": tick_data.get("session"),
            }

            logger.info("[DBSEC] K200 Futures tick: update received")

            logger.debug(
                "[DBSEC] Tick summary (redacted): %s",
                redact_dict(tick_summary),
            )

            if logger.isEnabledFor(logging.DEBUG):
                debug_snapshot = {
                    key: value
                    for key, value in tick_data.items()
                    if key != "raw_data"
                }
                logger.debug(
                    "[DBSEC] Tick snapshot (redacted): %s",
                    redact_dict(debug_snapshot),
                )

                raw_payload = tick_data.get("raw_data")
                if isinstance(raw_payload, dict):
                    logger.debug(
                        "[DBSEC] Raw tick payload (redacted): %s",
                        redact_dict(raw_payload),
                    )

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
            
            # Store daily open price for % calculation
            if not self.daily_open_price:
                self.daily_open_price = float(body.get("stck_oprc", current_price))  # 시가
                
            # Determine trading session
            session = self._determine_session()
            
            # Calculate change rate from daily open
            change_rate = 0.0
            if self.daily_open_price and self.daily_open_price > 0:
                change_rate = ((current_price - self.daily_open_price) / self.daily_open_price) * 100
            
            tick = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": "K200_FUT",
                "price": current_price,
                "volume": int(body.get("cntg_vol", 0)),  # 체결량
                "session": session,
                "change_rate": change_rate,
                "raw_data": raw_data
            }
            
            self.last_price = current_price
            self.current_session = session
            
            return tick
            
        except Exception as e:
            logger.error(f"Tick parsing error: {e}")
            return None
            
    def _determine_session(self) -> str:
        """Determine if current time is DAY or NIGHT session"""
        session = determine_trading_session()

        if session in {"DAY", "NIGHT"} and session != self.current_session:
            # 세션 전환 시 일중 기준가 리셋
            self.daily_open_price = None

        return session
            
    async def _check_anomaly(self, tick: Dict[str, Any]):
        """Check for price anomalies and trigger alerts"""
        change_rate = abs(tick.get("change_rate", 0))
        
        # Determine alert level based on change rate
        alert_level = None
        if change_rate >= self.alert_threshold:  # >= 1.0%
            alert_level = "CRITICAL"
        elif change_rate >= self.warn_threshold:  # >= 0.5%
            alert_level = "WARN"
        else:
            alert_level = "INFO"
        
        # Only send alerts for WARN or higher
        if alert_level in ["WARN", "CRITICAL"]:
            # Avoid spam: don't alert more than once per minute for same level
            now = datetime.now(timezone.utc)
            if (self.last_alert_time and 
                (now - self.last_alert_time).total_seconds() < 60):
                return
                
            self.last_alert_time = now
            
            # Create alert payload for MarketWatcher integration
            alert_payload = {
                "symbol": "K200_FUT",
                "session": tick["session"],
                "change": tick["change_rate"],
                "price": tick["price"],
                "timestamp": tick["timestamp"],
                "level": alert_level
            }
            
            logger.warning(f"ANOMALY DETECTED: K200_FUT {tick['change_rate']:.2f}% change in {tick['session']} session - Level: {alert_level}")
            
            # Send to MarketWatcher via Sentinel alert endpoint
            await self._send_to_market_watcher(alert_payload)
            
            # Call custom callback if set
            if self.alert_callback:
                try:
                    self.alert_callback(alert_payload)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")
                    
    async def _send_to_market_watcher(self, payload: Dict[str, Any]):
        """Send alert to MarketWatcher via Sentinel alert endpoint"""
        if not self.sentinel_base_url:
            logger.warning("SENTINEL_BASE_URL not configured, skipping MarketWatcher notification")
            return
            
        try:
            # Format payload for MarketWatcher
            # MarketWatcher expects the same format as other market alerts
            market_alert = {
                "index": "K200 선물",  # Display name
                "symbol": "K200_FUT",
                "level": self._grade_level(payload["change"]),
                "delta_pct": round(payload["change"], 2),
                "triggered_at": payload["timestamp"],
                "note": f"{payload['session']} 세션 - {'상승' if payload['change'] > 0 else '하락'} {abs(payload['change']):.2f}%",
                "kind": "FUTURES",
                "details": {
                    "session": payload["session"],
                    "price": payload["price"],
                    "change_pct": payload["change"]
                }
            }
            
            # Send via HTTP POST to maintain consistency with market_watcher.py
            url = f"{self.sentinel_base_url}/sentinel/alert"
            headers = {"Content-Type": "application/json"}
            if self.sentinel_key:
                headers["x-sentinel-key"] = self.sentinel_key
            
            # Use synchronous requests for compatibility
            response = requests.post(
                url,
                headers=headers,
                json=market_alert,
                timeout=10
            )
            
            if response.ok:
                logger.info(f"Alert sent to MarketWatcher: Level {market_alert['level']}")
            else:
                logger.error(f"MarketWatcher error: {response.status_code} - {response.text}")
                    
        except Exception as e:
            logger.error(f"Failed to send alert to MarketWatcher: {e}")
    
    def _grade_level(self, change_pct: float) -> str:
        """Grade alert level based on change percentage (same logic as market_watcher)"""
        abs_change = abs(change_pct)
        if abs_change >= 2.5:
            return "LV3"
        elif abs_change >= 1.5:
            return "LV2"
        elif abs_change >= 0.8:
            return "LV1"
        return None
            
    def get_recent_ticks(self, limit: Optional[int] = None) -> list:
        """Get recent tick data from buffer"""
        if not self.enabled:
            return []  # Return empty in mock mode
            
        if limit is None:
            limit = len(self.tick_buffer)
        return list(self.tick_buffer)[-limit:]
        
    def get_health_status(self) -> Dict[str, Any]:
        """Get WebSocket connection and monitoring health status"""
        if not self.enabled:
            return {
                "enabled": False,
                "mode": "MOCK",
                "connected": False,
                "status": "DISABLED - Mock mode for local development"
            }
            
        return {
            "enabled": True,
            "mode": "PRODUCTION",
            "connected": self.is_connected,
            "reconnect_attempts": self.reconnect_attempts,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "buffer_size": len(self.tick_buffer),
            "max_buffer_size": self.buffer_size,
            "last_price": self.last_price,
            "current_session": self.current_session,
            "alert_threshold": self.alert_threshold,
            "warn_threshold": self.warn_threshold,
            "last_alert_time": self.last_alert_time.isoformat() if self.last_alert_time else None
        }
        
    async def stop_monitoring(self):
        """Stop WebSocket monitoring"""
        self.is_connected = False
        if self.websocket:
            try:
                await asyncio.to_thread(self.websocket.close)
            finally:
                self.websocket = None
        logger.info("WebSocket monitoring stopped")


# Global monitor instance
_futures_monitor: Optional[KOSPI200FuturesMonitor] = None


def get_futures_monitor() -> KOSPI200FuturesMonitor:
    """Get global KOSPI200 futures monitor instance"""
    global _futures_monitor
    
    if _futures_monitor is None:
        # Check if DB증권 module is enabled
        dbsec_enabled = os.getenv("DBSEC_ENABLE", "true").lower() in ["true", "1", "yes", "on"]
        
        if not dbsec_enabled:
            logger.info("[DB증권] K200 Futures Monitor DISABLED by DBSEC_ENABLE=false")
        
        try:
            alert_threshold = float(os.getenv("DB_ALERT_THRESHOLD", "1.0").strip())
            warn_threshold = float(os.getenv("DB_WARN_THRESHOLD", "0.5").strip())
            buffer_size = int(os.getenv("DB_BUFFER_SIZE", "100").strip())
            ws_url = os.getenv("DB_WS_URL", "wss://openapi.dbsec.co.kr:9443/ws").strip()
            
            _futures_monitor = KOSPI200FuturesMonitor(
                alert_threshold=alert_threshold,
                warn_threshold=warn_threshold,
                buffer_size=buffer_size,
                ws_url=ws_url,
                enabled=dbsec_enabled
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to initialize futures monitor: {e}")
            # Create a default monitor as fallback
            _futures_monitor = KOSPI200FuturesMonitor(enabled=False)
    
    return _futures_monitor


async def start_futures_monitoring():
    """Start the global futures monitor"""
    monitor = get_futures_monitor()
    # Start monitoring in background task
    asyncio.create_task(monitor.start_monitoring())
    if monitor.enabled:
        logger.info("[DB증권] K200 Futures monitoring started in PRODUCTION mode")
    else:
        logger.info("[DB증권] K200 Futures monitoring started in MOCK mode")


async def stop_futures_monitoring():
    """Stop the global futures monitor"""
    global _futures_monitor
    if _futures_monitor:
        await _futures_monitor.stop_monitoring()
        _futures_monitor = None
    logger.info("[DB증권] K200 Futures monitoring stopped")