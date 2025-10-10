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
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import websocket
from websocket import WebSocketException
from websocket._exceptions import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)
import requests

from utils.masking import mask_secret, redact_headers, redact_ws_url, redact_dict
from utils.token_manager import get_token_manager
from utils.trading_session import determine_trading_session, is_krx_trading_day, KST

logger = logging.getLogger(__name__)

SUBSCRIBE_INFO_MESSAGE = "[DBSEC] Sent subscribe_msg for K200 Futures"
TICK_INFO_PREFIX = "[DBSEC] K200 Futures tick:"


def is_trading_session(now: Optional[datetime] = None) -> bool:
    """Return True when KOSPI200 futures trading is active."""
    status = determine_trading_session(now)
    return status.get("session") in {"DAY", "NIGHT"}
class KOSPI200FuturesMonitor:
    """KOSPI200 Futures real-time monitor with anomaly detection"""
    
    def __init__(
        self,
        alert_threshold: float = 1.5,  # 1.5% change threshold for CRITICAL (다른 지표와 동일)
        warn_threshold: float = 0.8,   # 0.8% change threshold for WARN (다른 지표와 동일)
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
        self.current_session: Optional[str] = None
        self.previous_session: Optional[str] = None
        
        # MarketWatcher integration endpoint (uses main Sentinel URL)
        self.sentinel_base_url = os.getenv("SENTINEL_BASE_URL", "").strip()
        self.sentinel_key = os.getenv("SENTINEL_KEY", "").strip()

        poll_minutes_env = os.getenv("DBSEC_POLL_MINUTES", "").strip()
        self.poll_minutes: int = 30  # 30분마다 재확인
        if poll_minutes_env:
            try:
                self.poll_minutes = max(1, int(poll_minutes_env))
            except ValueError:
                logger.warning(
                    "Invalid DBSEC_POLL_MINUTES value %s, using default 30 minutes",
                    poll_minutes_env,
                )

        sleep_cap_env = os.getenv("DBSEC_SLEEP_CAP_HOURS", "").strip()
        self.sleep_cap_hours: Optional[int] = None
        if sleep_cap_env:
            try:
                parsed_hours = int(sleep_cap_env)
                if parsed_hours > 0:
                    self.sleep_cap_hours = parsed_hours
            except ValueError:
                logger.warning(
                    "Invalid DBSEC_SLEEP_CAP_HOURS value %s, ignoring cap",
                    sleep_cap_env,
                )

        self._last_holiday_notice: Optional[datetime] = None
        
    def set_alert_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback function for alert notifications"""
        self.alert_callback = callback

    @staticmethod
    def _format_kst(target: datetime) -> str:
        """Format datetime in KST for user-facing logs."""
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return target.astimezone(KST).strftime("%Y-%m-%d %H:%M %Z")

    def _log_closed_wait(self) -> None:
        """Emit standardized debug log for closed market waits."""
        logger.debug("[DBSEC] 비거래 시간, 30분 후 재확인")

    async def _sleep_until_poll(self):
        """Sleep for the configured poll interval with a single debug log."""
        total_seconds = max(1, int(self.poll_minutes * 60))
        self._log_closed_wait()

        try:
            await asyncio.wait_for(asyncio.sleep(total_seconds), timeout=total_seconds)
        except asyncio.TimeoutError:
            logger.warning("[DBSEC] WebSocket timeout, retrying...")

    def _update_session_state(self, session: Optional[str]) -> None:
        """Track session transitions and emit informational logs on changes."""
        if not session:
            return

        if session != self.current_session:
            previous = self.current_session
            self.previous_session = previous
            self.current_session = session

            if session in {"DAY", "NIGHT", "CLOSED"}:
                logger.info(
                    "[DBSEC] Trading session changed from %s to %s",
                    previous or "UNKNOWN",
                    session,
                )

    def _calculate_backoff_delay(self, base: int = 2, cap: int = 60) -> int:
        """Compute exponential backoff delay with configurable base and cap."""
        exponent = max(0, self.reconnect_attempts - 1)
        delay = base * (2 ** exponent)
        return min(cap, delay)

    async def _apply_backoff(
        self,
        reason: str,
        *,
        base: int = 2,
        cap: int = 60,
        log_level: int = logging.WARNING,
    ) -> None:
        """Increment attempts, log, and sleep for the computed backoff delay."""
        self.reconnect_attempts += 1
        delay = self._calculate_backoff_delay(base=base, cap=cap)

        logger.log(
            log_level,
            "[DBSEC] %s (attempt %s) — %ss backoff",
            reason,
            self.reconnect_attempts,
            delay,
        )
        await asyncio.sleep(delay)

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
            status = determine_trading_session()
            session = status.get("session")
            self._update_session_state(session)
            is_holiday = bool(status.get("is_holiday"))

            # If it's a holiday (weekend or KRX holiday), skip WebSocket connection
            if is_holiday:
                self.is_connected = False
                self.reconnect_attempts = 0
                logger.info("[DBSEC] 휴장일 - 30분 후 재확인")
                await self._sleep_until_poll()
                continue

            # For CLOSED session on trading days, just wait briefly and recheck
            # This handles the gaps between DAY and NIGHT sessions  
            if session == "CLOSED":
                self.is_connected = False
                self.reconnect_attempts = 0
                logger.debug("[DBSEC] 거래시간 외 - 30분 후 재확인")
                await self._sleep_until_poll()
                continue

            # Session is DAY or NIGHT - connect WebSocket immediately
            try:
                await self._connect_and_monitor()

            except asyncio.TimeoutError:
                self.is_connected = False
                # 타임아웃 시 더 긴 대기 시간 (30분)
                logger.warning("[DBSEC] WebSocket timeout - waiting 30 minutes before retry")
                await self._sleep_until_poll()
                continue

            except (WebSocketConnectionClosedException, WebSocketException) as exc:
                self.is_connected = False
                logger.warning(f"[DBSEC] WebSocket connection lost: {exc} - waiting 30 minutes")
                await self._sleep_until_poll()
                continue

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.is_connected = False
                error_msg = str(exc)
                logger.error(f"Monitoring error: {error_msg}")

                if "token" in error_msg.lower() or "access" in error_msg.lower():
                    await self._apply_backoff(
                        "Token-related error",
                        base=60,
                        cap=1800,
                    )
                else:
                    await self._apply_backoff("Unexpected monitoring error", base=2, cap=60)
        
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

        logger.debug("Connecting to WebSocket: %s", redact_ws_url(ws_url))

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
            timeout=60,  # 연결 타임아웃을 60초로 증가
            enable_multithread=True,
        )

        self.websocket = ws
        self.is_connected = True
        self.reconnect_attempts = 0

        logger.info("WebSocket connected successfully")

        # Ensure recv does not block forever
        await asyncio.to_thread(ws.settimeout, 60)  # recv 타임아웃도 60초로

        try:
            # Subscribe to KOSPI200 futures
            await self._subscribe_futures()

            # Send periodic ping to keep connection alive
            ping_task = asyncio.create_task(self._ping_loop(ws))
            
            try:
                # Listen for messages
                while self.is_connected:
                    try:
                        message = await asyncio.to_thread(ws.recv)
                    except WebSocketTimeoutException:
                        # Send ping on timeout
                        try:
                            await asyncio.to_thread(ws.ping)
                            logger.debug("[DBSEC] Sent ping to keep connection alive")
                        except:
                            pass
                        await asyncio.sleep(5)
                        continue

                    if message is None:
                        continue

                    if isinstance(message, bytes):
                        message = message.decode("utf-8", "ignore")

                    await self._handle_message(message)
            finally:
                ping_task.cancel()
        except WebSocketConnectionClosedException as exc:
            raise exc
        finally:
            self.is_connected = False
            if self.websocket:
                try:
                    await asyncio.to_thread(self.websocket.close)
                finally:
                    self.websocket = None
    
    async def _ping_loop(self, ws):
        """Send periodic pings to keep WebSocket connection alive"""
        while self.is_connected:
            try:
                await asyncio.sleep(30)  # Ping every 30 seconds
                if self.is_connected and ws:
                    await asyncio.to_thread(ws.ping)
                    logger.debug("[DBSEC] Periodic ping sent")
            except:
                break
                
    async def _subscribe_futures(self):
        """Subscribe to KOSPI200 futures real-time data"""
        # DB증권 API 실시간 선물 구독 명세 기반 메시지 구성
        # K200 선물의 실제 종목코드 사용
        fut_code = os.getenv("DB_FUTURES_CODE", "101V3000").strip()  # K200 선물 종목코드 
        tr_id = os.getenv("DB_FUTURES_TR_ID", "HDFSCNT0").strip() or "HDFSCNT0"
        tr_key = fut_code  # 종목코드를 tr_key로 사용

        subscribe_msg = {
            "header": {
                "tr_type": "1",          # 1: 실시간 등록, 2: 실시간 해제
                "content_type": "utf-8",  # 명세에 따른 인코딩 정보
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": tr_key,  # 종목코드를 키로 사용
                }
            }
        }

        if not self.websocket:
            raise RuntimeError("WebSocket connection is not established")

        await asyncio.to_thread(self.websocket.send, json.dumps(subscribe_msg))
        logger.info(SUBSCRIBE_INFO_MESSAGE)
        
    async def _handle_message(self, message: str):
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)
            
            # Log raw message structure for debugging (first few times)
            if len(self.tick_buffer) < 5:
                logger.debug(f"[DBSEC] Raw message keys: {list(data.keys())}")
                if 'header' in data:
                    logger.debug(f"[DBSEC] Header: {data['header']}")
            
            # Parse futures data (format depends on DB증권 API)
            tick_data = await self._parse_tick_data(data)
            if not tick_data:
                logger.debug("[DBSEC] No valid tick data parsed from message")
                return
                
            # Add to buffer
            self.tick_buffer.append(tick_data)

            logger.info(f"{TICK_INFO_PREFIX} Price: {tick_data['price']:.2f}, Change: {tick_data['change_rate']:.2f}%")

            if logger.isEnabledFor(logging.DEBUG):
                tick_summary = {
                    "price": tick_data.get("price"),
                    "change_rate": tick_data.get("change_rate"),
                    "volume": tick_data.get("volume"),
                    "session": tick_data.get("session"),
                }

                logger.debug(
                    "%s summary (redacted): %s",
                    TICK_INFO_PREFIX,
                    redact_dict(tick_summary),
                )

                debug_snapshot = {
                    key: value
                    for key, value in tick_data.items()
                    if key != "raw_data"
                }
                logger.debug(
                    "%s snapshot (redacted): %s",
                    TICK_INFO_PREFIX,
                    redact_dict(debug_snapshot),
                )

                raw_payload = tick_data.get("raw_data")
                if isinstance(raw_payload, dict):
                    logger.debug(
                        "%s raw payload (redacted): %s",
                        TICK_INFO_PREFIX,
                        redact_dict(raw_payload),
                    )

            # Check for anomalies
            await self._check_anomaly(tick_data)
            
        except Exception as e:
            logger.error(f"Message handling error: {e}")
            
    async def _parse_tick_data(self, raw_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse raw WebSocket data into standardized tick format"""
        try:
            # DB증권 실시간 데이터 형식
            # header와 body로 구분되며, body.output에 실제 데이터가 있음
            header = raw_data.get("header", {})
            body = raw_data.get("body", {})
            
            # tr_id로 메시지 타입 확인
            tr_id = header.get("tr_id", "")
            if not tr_id:
                logger.debug("No tr_id in message, skipping")
                return None
            
            # 실시간 선물 체결 데이터 처리
            output = body.get("output", body)  # output이 없으면 body 자체 사용
            
            # 다양한 필드명 시도 (DB증권 API 문서 기반)
            current_price = 0.0
            price_fields = ["fut_prpr", "stck_prpr", "prpr", "price", "현재가"]
            for field in price_fields:
                if field in output:
                    try:
                        current_price = float(output[field])
                        if current_price > 0:
                            break
                    except (ValueError, TypeError):
                        continue
            
            if current_price <= 0:
                logger.debug(f"No valid price found in data: {output.keys()}")
                return None
            
            # Store daily open price for % calculation
            if not self.daily_open_price:
                open_fields = ["fut_oprc", "stck_oprc", "oprc", "open", "시가"]
                for field in open_fields:
                    if field in output:
                        try:
                            self.daily_open_price = float(output[field])
                            if self.daily_open_price > 0:
                                break
                        except (ValueError, TypeError):
                            continue
                
                # If no open price, use current price
                if not self.daily_open_price:
                    self.daily_open_price = current_price
                    
            # Determine trading session
            session = self._determine_session()
            
            # Calculate change rate from daily open
            change_rate = 0.0
            if self.daily_open_price and self.daily_open_price > 0:
                change_rate = ((current_price - self.daily_open_price) / self.daily_open_price) * 100
            
            # Extract volume
            volume = 0
            volume_fields = ["cntg_vol", "acml_vol", "vol", "volume", "체결량"]
            for field in volume_fields:
                if field in output:
                    try:
                        volume = int(output[field])
                        if volume > 0:
                            break
                    except (ValueError, TypeError):
                        continue
            
            tick = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": "K200_FUT",
                "price": current_price,
                "volume": volume,
                "session": session,
                "change_rate": change_rate,
                "raw_data": raw_data
            }
            
            self.last_price = current_price
            self.current_session = session
            
            logger.info(f"[DBSEC] Parsed tick: price={current_price:.2f}, change={change_rate:.2f}%, session={session}")
            
            return tick
            
        except Exception as e:
            logger.error(f"Tick parsing error: {e}, raw_data keys: {raw_data.keys() if raw_data else 'None'}")
            return None
            
    def _determine_session(self) -> str:
        """Determine if current time is DAY or NIGHT session"""
        status = determine_trading_session()
        session = status.get("session", "CLOSED")

        if session in {"DAY", "NIGHT"} and session != self.current_session:
            # 세션 전환 시 일중 기준가 리셋
            self.daily_open_price = None

        return session
            
    async def _check_anomaly(self, tick: Dict[str, Any]):
        """Check for price anomalies and trigger alerts"""
        change_rate = tick.get("change_rate", 0)
        abs_change = abs(change_rate)
        
        # Grade level using market_watcher standards
        level = self._grade_level(change_rate)
        
        # Only send alerts if level is determined (>= 0.8%)
        if level:
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
                "change": change_rate,  # Use original signed value
                "price": tick["price"],
                "timestamp": tick["timestamp"],
                "level": level
            }
            
            logger.warning(f"ANOMALY DETECTED: K200_FUT {change_rate:.2f}% change in {tick['session']} session - Level: {level}")
            
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
        # market_watcher.py와 동일한 기준 적용
        if abs_change >= 2.5:
            return "LV3"
        elif abs_change >= 1.5:
            return "LV2"
        elif abs_change >= 0.8:
            return "LV1"
        return None  # 0.8% 미만은 알림 없음
            
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
            alert_threshold = float(os.getenv("DB_ALERT_THRESHOLD", "1.5").strip())  # 다른 지표와 동일
            warn_threshold = float(os.getenv("DB_WARN_THRESHOLD", "0.8").strip())   # 다른 지표와 동일
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