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
from typing import Optional, Dict, Any, Callable, Deque, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import holidays
import pytz
import requests
import websocket
from websocket import WebSocketException
from websocket._exceptions import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)

from utils.token_manager import get_token_manager

logger = logging.getLogger(__name__)


SENSITIVE_KEYS = {"token", "appkey", "app_secret", "appsecret", "authorization"}


def mask_secret(value: str, head: int = 4, tail: int = 4) -> str:
    """Mask a sensitive string while preserving leading and trailing characters."""
    if not isinstance(value, str):
        return value
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}{'*' * (len(value) - head - tail)}{value[-tail:]}"


def redact_kv(key: str, value: Any) -> str:
    """Redact sensitive key-value pairs based on known key names."""
    try:
        key_lower = key.lower()
    except AttributeError:
        key_lower = str(key).lower()
    if key_lower in SENSITIVE_KEYS:
        return mask_secret(str(value))
    return str(value)


def redact_ws_url(url: str) -> str:
    """Redact sensitive query parameters in a WebSocket URL."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        redacted_query = [(k, redact_kv(k, v)) for k, v in query_pairs]
        return urlunparse(parsed._replace(query=urlencode(redacted_query)))
    except Exception:
        return "<redacted-url>"


def redact_headers(headers: Any) -> Any:
    """Redact sensitive values inside WebSocket headers."""
    try:
        if isinstance(headers, list):
            sanitized = []
            for line in headers:
                if ":" not in line:
                    sanitized.append(line)
                    continue
                key, value = line.split(":", 1)
                prefix = " " if value.startswith(" ") else ""
                sanitized.append(f"{key}:{prefix}{redact_kv(key.strip(), value.strip())}")
            return sanitized
        if isinstance(headers, dict):
            return {k: redact_kv(k, v) for k, v in headers.items()}
    except Exception:
        return headers
    return headers


def redact_dict(obj: Any) -> Any:
    """Recursively redact sensitive values within dictionaries and lists."""
    try:
        if isinstance(obj, dict):
            redacted: Dict[Any, Any] = {}
            for key, value in obj.items():
                key_lower = str(key).lower()
                if key_lower in SENSITIVE_KEYS:
                    redacted[key] = mask_secret(str(value))
                elif isinstance(value, (dict, list)):
                    redacted[key] = redact_dict(value)
                else:
                    redacted[key] = value
            return redacted
        if isinstance(obj, list):
            return [redact_dict(item) if isinstance(item, (dict, list)) else item for item in obj]
    except Exception:
        return obj
    return obj


class KOSPI200FuturesMonitor:
    """KOSPI200 Futures real-time monitor with anomaly detection"""
    
    def __init__(
        self,
        alert_threshold: float = 1.0,  # 1% change threshold for CRITICAL
        warn_threshold: float = 0.5,   # 0.5% change threshold for WARN
        buffer_size: int = 100,
        ws_url: Optional[str] = None,
        enabled: bool = True
    ):
        self.enabled = enabled
        
        # Strip whitespace and validate WebSocket URL
        resolved_ws_url = ws_url or os.getenv("DB_WS_URL", "wss://openapi.dbsec.co.kr:9443/ws")
        cleaned_ws_url = resolved_ws_url.strip() if resolved_ws_url else ""
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
        self.max_reconnect_attempts: int = 10 if enabled else 0
        self.last_safe_ws_url: Optional[str] = None
        
        # Event handlers
        self.alert_callback: Optional[Callable] = None
        
        # Session tracking
        self.current_session: str = "UNKNOWN"  # DAY or NIGHT
        self.krx_holidays = holidays.KR()
        self.kst = pytz.timezone("Asia/Seoul")

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
        
        while self.reconnect_attempts < self.max_reconnect_attempts:
            try:
                await self._connect_and_monitor()
                
            except (WebSocketConnectionClosedException, WebSocketException) as e:
                self.reconnect_attempts += 1
                logger.warning(
                    "WebSocket connection lost (attempt %s): %s",
                    self.reconnect_attempts,
                    e,
                )

                if self.reconnect_attempts < self.max_reconnect_attempts:
                    # Exponential backoff: 2^attempt seconds (max 60s)
                    backoff = min(2 ** self.reconnect_attempts, 60)
                    logger.info("Reconnecting in %s seconds...", backoff)
                    await asyncio.sleep(backoff)
                else:
                    logger.error("Max reconnection attempts exceeded")
                    break
                    
            except Exception as e:
                error_msg = str(e)
                logger.error(
                    "Monitoring error: %s (url=%s)",
                    error_msg,
                    self.last_safe_ws_url or redact_ws_url(self.ws_url),
                )
                
                # Handle token-related errors with longer backoff
                if "token" in error_msg.lower() or "access" in error_msg.lower():
                    self.reconnect_attempts += 1
                    # Longer backoff for token issues: 60s, 120s, 300s, etc.
                    backoff = min(60 * (2 ** (self.reconnect_attempts - 1)), 1800)  # Max 30min
                    logger.warning(
                        "Token-related error, waiting %ss before retry...",
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
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
        query_params.update(
            {
                "appkey": app_key,
                "token": access_token,
            }
        )
        ws_url = urlunparse(parsed_url._replace(query=urlencode(query_params)))
        safe_ws_url = redact_ws_url(ws_url)

        self.last_safe_ws_url = safe_ws_url
        logger.info("Connecting to WebSocket: %s", safe_ws_url)

        headers: Dict[str, str] = {}
        send_auth_header = os.getenv("DBSEC_WS_SEND_AUTH_HEADER", "false").lower() in ("1", "true", "yes")
        if send_auth_header:
            token_type = getattr(token_manager, "token_type", "Bearer")
            headers["Authorization"] = f"{token_type} {access_token}"
            logger.debug("Including Authorization header for WebSocket handshake")

        header_list = [f"{k}: {v}" for k, v in headers.items()]

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("WS headers: %s", redact_headers(header_list))

        try:
            ws = await asyncio.to_thread(
                websocket.create_connection,
                ws_url,
                header=header_list,
                timeout=30,
                enable_multithread=True,
            )
        except Exception as exc:
            logger.error("WS connect failed: %s url=%s", exc, safe_ws_url)
            raise

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
                    # Keep the loop alive if no data arrives in timeout window
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
        tr_id = os.getenv("DB_FUTURES_TR_ID", "H0IFCNI0").strip() or "H0IFCNI0"
        custtype = os.getenv("DB_FUTURES_CUSTTYPE", "P").strip() or "P"
        tr_key = os.getenv("DB_FUTURES_TR_KEY", "101QC000").strip() or "101QC000"
        tr_type = os.getenv("DB_FUTURES_TR_TYPE", "1").strip() or "1"

        subscribe_msg = {
            "header": {
                "tr_id": tr_id,        # 실시간 구독 TR ID (선물 체결가)
                "custtype": custtype,  # 고객 구분 P=개인, B=법인
            },
            "body": {
                "input": {
                    "tr_key": tr_key,  # 선물 종목코드 (예: 101QC000 K200 최근월)
                    "tr_type": tr_type  # 1=구독, 2=해지
                }
            }
        }

        if not self.websocket:
            raise RuntimeError("WebSocket connection is not established")

        payload = json.dumps(subscribe_msg, ensure_ascii=False)
        await asyncio.to_thread(self.websocket.send, payload)
        logger.info("[DB증권] Sent subscribe_msg(tr_id=%s, tr_key=%s)", tr_id, tr_key)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[DB증권] Subscribe payload: %s", payload)

    async def _handle_message(self, message: str):
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)
            if logger.isEnabledFor(logging.DEBUG):
                safe_debug_payload = redact_dict(data)
                logger.debug(
                    "[DB증권] Raw message: %s",
                    json.dumps(safe_debug_payload, ensure_ascii=False),
                )

            # Parse futures data (format depends on DB증권 API)
            tick_data = await self._parse_tick_data(data)
            if not tick_data:
                return

            # Add to buffer
            self.tick_buffer.append(tick_data)

            # Check for anomalies
            await self._check_anomaly(tick_data)

            safe_tick = redact_dict(tick_data)
            logger.info(
                "[DB증권] K200 Futures tick: %s",
                json.dumps(safe_tick, ensure_ascii=False),
            )

        except Exception as e:
            logger.error("Message handling error: %s", e)
            if logger.isEnabledFor(logging.DEBUG):
                safe_snapshot = None
                try:
                    if isinstance(message, str):
                        safe_snapshot = json.dumps(
                            redact_dict(json.loads(message)),
                            ensure_ascii=False,
                        )
                    elif isinstance(message, (dict, list)):
                        safe_snapshot = json.dumps(
                            redact_dict(message),
                            ensure_ascii=False,
                        )
                except Exception:
                    safe_snapshot = None

                if safe_snapshot:
                    logger.debug("[DB증권] Last message snapshot: %s", safe_snapshot)

    async def _parse_tick_data(self, raw_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse raw WebSocket data into standardized tick format"""
        try:
            body = raw_data.get("body", {})
            output = body.get("output") or body.get("body") or body

            # DB증권 실시간 선물 체결 데이터에서 사용 가능한 가격 필드 후보
            price_fields = [
                "futs_prpr",  # 선물 현재가
                "stck_prpr",  # 주식 현재가 (fallback)
                "last",       # 일반화된 현재가
            ]
            open_fields = [
                "futs_oprc",
                "stck_oprc",
                "open",
            ]
            volume_fields = [
                "cntg_vol",
                "acml_vol",
                "volume",
            ]

            current_price = self._extract_numeric(output, price_fields)
            if current_price is None or current_price <= 0:
                return None

            if not self.daily_open_price:
                open_price = self._extract_numeric(output, open_fields)
                self.daily_open_price = open_price if open_price and open_price > 0 else current_price

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
                "volume": int(self._extract_numeric(output, volume_fields) or 0),  # 체결량
                "session": session,
                "change_rate": change_rate,
                "raw_data": raw_data
            }

            self.last_price = current_price
            self.current_session = session
            
            return tick
            
        except Exception as e:
            logger.error("Tick parsing error: %s", e)
            return None
            
    def _determine_session(self) -> str:
        """Determine if current time is DAY or NIGHT session"""
        from datetime import datetime, time

        now = datetime.now(self.kst)
        current_time = now.time()

        # KOSPI200 futures trading hours (KST)
        # Day session: 09:00 - 15:45 (includes closing auction)
        # Night session: 18:00 - 06:00 (next day)

        day_start = time(9, 0)   # 09:00
        day_end = time(15, 45)   # 15:45
        night_start = time(18, 0) # 18:00
        night_end = time(6, 0)    # 06:00 (next day)

        # 휴장일 확인
        if now.date() in self.krx_holidays:
            return "CLOSED"

        # Reset daily open at session start
        if current_time == day_start or current_time == night_start:
            self.daily_open_price = None

        if day_start <= current_time <= day_end:
            return "DAY"
        elif current_time >= night_start or current_time <= night_end:
            return "NIGHT"
        else:
            return "CLOSED"

    def _extract_numeric(self, payload: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
        """Safely extract numeric value from payload using preferred key order."""
        for key in keys:
            if key in payload:
                try:
                    value = payload[key]
                    if isinstance(value, (int, float)):
                        return float(value)
                    if isinstance(value, str) and value.strip():
                        return float(value.replace(",", ""))
                except (TypeError, ValueError):
                    continue
        return None
            
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
            
            logger.warning(
                "ANOMALY DETECTED: K200_FUT %.2f%% change in %s session - Level: %s",
                tick.get("change_rate", 0.0),
                tick.get("session", "UNKNOWN"),
                alert_level,
            )
            
            # Send to MarketWatcher via Sentinel alert endpoint
            await self._send_to_market_watcher(alert_payload)
            
            # Call custom callback if set
            if self.alert_callback:
                try:
                    self.alert_callback(alert_payload)
                except Exception as e:
                    logger.error("Alert callback error: %s", e)
                    
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
                logger.info(
                    "Alert sent to MarketWatcher: Level %s",
                    market_alert.get("level"),
                )
            else:
                logger.error(
                    "MarketWatcher error: %s - %s",
                    response.status_code,
                    response.text,
                )
                    
        except Exception as e:
            logger.error("Failed to send alert to MarketWatcher: %s", e)
    
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
            logger.error("Failed to initialize futures monitor: %s", e)
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
