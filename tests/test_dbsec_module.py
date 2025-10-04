"""
Unit tests for DB증권 API module
Tests token manager, WebSocket service, and MarketWatcher integration
"""
import pytest
import asyncio
import json
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from websocket import WebSocketException

# Mock environment variables before importing modules
import os
os.environ['DB_APP_KEY'] = 'test_app_key'
os.environ['DB_APP_SECRET'] = 'test_app_secret'
os.environ['DB_API_BASE'] = 'https://test.dbsec.co.kr:8443'
os.environ['DB_WS_URL'] = 'wss://test.dbsec.co.kr:9443/ws'
os.environ['SENTINEL_BASE_URL'] = 'https://test.sentinel.com'
os.environ['SENTINEL_KEY'] = 'test_sentinel_key'

from utils.token_manager import DBSecTokenManager, get_token_manager
from services.dbsec_ws import (
    KOSPI200FuturesMonitor,
    get_futures_monitor,
    mask_secret,
)
from app.utils import determine_trading_session


class TestTokenManager:
    """Test cases for DB증권 Token Manager"""
    
    @pytest.mark.asyncio
    async def test_token_refresh_success(self):
        """Test successful token refresh"""
        manager = DBSecTokenManager("test_key", "test_secret")
        
        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "test_token_12345",
            "expires_in": 86400,
            "token_type": "Bearer"
        }
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_client.return_value.__aenter__.return_value = mock_instance
            
            result = await manager._refresh_token()
            
            assert result == True
            assert manager.access_token == "test_token_12345"
            assert manager.token_type == "Bearer"
            assert manager.expires_at is not None
    
    @pytest.mark.asyncio
    async def test_token_refresh_403_error(self):
        """Test token refresh with 403 error"""
        manager = DBSecTokenManager("test_key", "test_secret")
        
        # Mock 403 response
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.text = "Content-Type이 유효하지 않습니다"
        mock_response.json.side_effect = json.JSONDecodeError("error", "", 0)
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_client.return_value.__aenter__.return_value = mock_instance
            
            result = await manager._refresh_token()
            
            assert result == False
            assert manager.access_token is None
    
    @pytest.mark.asyncio
    async def test_token_auto_refresh(self):
        """Test automatic token refresh task"""
        manager = DBSecTokenManager("test_key", "test_secret")
        
        # Mock successful token fetch
        with patch.object(manager, '_refresh_token', new=AsyncMock(return_value=True)):
            await manager.start_auto_refresh()
            
            # Wait a bit for task to start
            await asyncio.sleep(0.1)
            
            assert manager._refresh_task is not None
            assert not manager._refresh_task.done()
            
            # Clean up
            await manager.stop_auto_refresh()
    
    def test_token_manager_singleton(self):
        """Test token manager singleton pattern"""
        manager1 = get_token_manager()
        manager2 = get_token_manager()
        
        assert manager1 is manager2  # Should be the same instance


class TestKOSPI200FuturesMonitor:
    """Test cases for KOSPI200 Futures WebSocket Monitor"""
    
    def test_monitor_initialization(self):
        """Test monitor initialization with environment variables"""
        monitor = KOSPI200FuturesMonitor(
            alert_threshold=1.0,
            warn_threshold=0.5,
            buffer_size=100,
            ws_url=os.getenv("DB_WS_URL")
        )
        
        assert monitor.alert_threshold == 1.0
        assert monitor.warn_threshold == 0.5
        assert monitor.buffer_size == 100
        expected_ws_url = os.getenv("DB_WS_URL", "wss://openapi.dbsec.co.kr:9443/ws")
        assert monitor.ws_url == expected_ws_url
        assert len(monitor.tick_buffer) == 0
    
    def test_determine_trading_session_helper(self, monkeypatch):
        """Verify the shared trading session helper covers day/night windows."""
        import app.utils as utils

        tz = ZoneInfo("Asia/Seoul")

        # 주간 세션 판정
        monkeypatch.setattr(utils, "is_krx_trading_day", lambda _: True)
        assert determine_trading_session(datetime(2024, 1, 2, 9, 0, tzinfo=tz)) == "DAY"
        assert determine_trading_session(datetime(2024, 1, 2, 15, 30, tzinfo=tz)) == "DAY"

        # 야간 세션 판정 (당일 저녁)
        monkeypatch.setattr(utils, "is_krx_trading_day", lambda _: True)
        assert determine_trading_session(datetime(2024, 1, 2, 18, 0, tzinfo=tz)) == "NIGHT"

        # 익일 새벽에는 전일 기준 휴장 여부 확인
        call_args = []

        def tracker(day):
            call_args.append(day)
            return True

        monkeypatch.setattr(utils, "is_krx_trading_day", tracker)
        assert determine_trading_session(datetime(2024, 1, 3, 2, 0, tzinfo=tz)) == "NIGHT"
        assert call_args[-1].isoformat() == "2024-01-02"

        # 휴장일에는 CLOSED 반환
        monkeypatch.setattr(utils, "is_krx_trading_day", lambda _: False)
        assert determine_trading_session(datetime(2024, 1, 2, 10, 0, tzinfo=tz)) == "CLOSED"
    
    @pytest.mark.asyncio
    async def test_parse_tick_data(self):
        """Test parsing of tick data"""
        monitor = KOSPI200FuturesMonitor()

        # Sample tick data (DB증권 API format)
        raw_data = {
            "body": {
                "stck_prpr": "350.50",  # Current price
                "stck_oprc": "348.00",  # Open price
                "cntg_vol": "12345"     # Volume
            }
        }
        
        tick = await monitor._parse_tick_data(raw_data)
        
        assert tick is not None
        assert tick["symbol"] == "K200_FUT"
        assert tick["price"] == 350.50
        assert tick["volume"] == 12345
        assert "timestamp" in tick
        assert "session" in tick

    @pytest.mark.asyncio
    async def test_session_change_resets_open_price(self, monkeypatch):
        """Ensure session transitions reset the cached open price."""
        monitor = KOSPI200FuturesMonitor()
        monitor.daily_open_price = 345.0
        monitor.current_session = "DAY"

        raw_data = {
            "body": {
                "stck_prpr": "360.00",
                "stck_oprc": "358.00",
                "cntg_vol": "1000",
            }
        }

        monkeypatch.setattr(
            "services.dbsec_ws.determine_trading_session",
            lambda: "NIGHT",
        )

        tick = await monitor._parse_tick_data(raw_data)

        assert monitor.current_session == "NIGHT"
        assert monitor.daily_open_price == pytest.approx(358.0)
        assert tick["session"] == "NIGHT"
        assert tick["change_rate"] == pytest.approx(((360.0 - 358.0) / 358.0) * 100)

    @pytest.mark.asyncio
    async def test_anomaly_detection(self):
        """Test anomaly detection and alert generation"""
        monitor = KOSPI200FuturesMonitor(
            alert_threshold=1.0,
            warn_threshold=0.5
        )
        
        # Mock MarketWatcher send
        with patch.object(monitor, '_send_to_market_watcher', new=AsyncMock()) as mock_send:
            # Test CRITICAL level (>= 1.0%)
            tick_critical = {
                "symbol": "K200_FUT",
                "session": "DAY",
                "change_rate": 1.5,
                "price": 355.0,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            await monitor._check_anomaly(tick_critical)
            mock_send.assert_called_once()
            
            # Verify alert payload
            call_args = mock_send.call_args[0][0]
            assert call_args["symbol"] == "K200_FUT"
            assert call_args["session"] == "DAY"
            assert call_args["change"] == 1.5
            assert call_args["level"] == "CRITICAL"
    
    def test_alert_level_grading(self):
        """Test alert level grading logic"""
        monitor = KOSPI200FuturesMonitor()
        
        # Test level grading (same as market_watcher.py)
        assert monitor._grade_level(0.3) == None
        assert monitor._grade_level(0.8) == "LV1"
        assert monitor._grade_level(1.5) == "LV2"
        assert monitor._grade_level(2.5) == "LV3"
        assert monitor._grade_level(-2.5) == "LV3"  # Test negative change
    
    def test_health_status(self):
        """Test health status reporting"""
        monitor = KOSPI200FuturesMonitor()
        
        health = monitor.get_health_status()
        
        assert "connected" in health
        assert "buffer_size" in health
        assert "last_price" in health
        assert "current_session" in health
        assert "alert_threshold" in health
        assert "warn_threshold" in health
        
        assert health["connected"] == False  # Not connected initially
        assert health["buffer_size"] == 0
        assert health["alert_threshold"] == 1.0


class TestMarketWatcherIntegration:
    """Test MarketWatcher integration for K200_FUT events"""
    
    @pytest.mark.asyncio
    async def test_market_watcher_alert_format(self):
        """Test that alerts are formatted correctly for MarketWatcher"""
        monitor = KOSPI200FuturesMonitor()
        monitor.sentinel_base_url = "https://test.sentinel.com"
        monitor.sentinel_key = "test_key"
        
        alert_payload = {
            "symbol": "K200_FUT",
            "session": "NIGHT",
            "change": -1.2,
            "price": 345.0,
            "timestamp": "2025-01-01T18:30:00Z",
            "level": "CRITICAL"
        }
        
        with patch('requests.post') as mock_post:
            mock_response = Mock()
            mock_response.ok = True
            mock_post.return_value = mock_response
            
            await monitor._send_to_market_watcher(alert_payload)
            
            # Verify the call
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            
            # Check URL
            assert call_args[0][0] == "https://test.sentinel.com/sentinel/alert"
            
            # Check headers
            headers = call_args[1]["headers"]
            assert headers["Content-Type"] == "application/json"
            assert headers["x-sentinel-key"] == "test_key"
            
            # Check payload format
            payload = call_args[1]["json"]
            assert payload["index"] == "K200 선물"
            assert payload["symbol"] == "K200_FUT"
            assert payload["level"] == "LV1"  # Based on 1.2% change
            assert payload["delta_pct"] == -1.2
            assert payload["kind"] == "FUTURES"
            assert payload["details"]["session"] == "NIGHT"
            assert "하락 1.20%" in payload["note"]
            assert "NIGHT 세션" in payload["note"]


class TestWebSocketReconnection:
    """Test WebSocket reconnection and error handling"""
    
    @pytest.mark.asyncio
    async def test_reconnect_on_connection_lost(self):
        """Test automatic reconnection on connection loss"""
        monitor = KOSPI200FuturesMonitor()
        monitor.max_reconnect_attempts = 2
        
        with patch('services.dbsec_ws.get_token_manager') as mock_token_mgr:
            # Mock token manager
            mock_tm = AsyncMock()
            mock_tm.get_token.return_value = "test_token"
            mock_tm._is_in_backoff.return_value = False
            mock_token_mgr.return_value = mock_tm

            with patch('services.dbsec_ws.determine_trading_session', return_value="DAY"), \
                 patch('services.dbsec_ws.websocket.create_connection') as mock_ws:
                # Simulate connection error then success
                mock_ws.side_effect = [
                    WebSocketException("Connection lost"),
                    MagicMock()  # Successful reconnection
                ]

                # Run monitoring (will attempt reconnect)
                task = asyncio.create_task(monitor.start_monitoring())

                # Give it time to attempt reconnection
                await asyncio.sleep(0.1)

                # Cancel the task
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                # Verify reconnection was attempted
                assert monitor.reconnect_attempts > 0


class TestSecretMasking:
    """Test masking utility for sensitive secrets."""

    def test_mask_secret_with_long_value(self):
        """Ensure long secrets reveal prefix and suffix only."""
        secret = "abcd1234ef"
        masked = mask_secret(secret)

        assert masked == secret[:4] + "***" + secret[-2:]

    def test_mask_secret_with_short_value(self):
        """Ensure short secrets remain fully masked."""
        secret = "12345"
        masked = mask_secret(secret)

        assert masked == "***"

    def test_mask_secret_threshold_behaviour(self):
        """Ensure secrets at the visibility threshold stay hidden."""
        secret = "abcd123xy"  # length 9 == 4 + 2 + 3
        masked = mask_secret(secret)

        assert masked == "***"

    def test_mask_secret_fixed_pattern(self):
        """Ensure custom head/tail arguments do not alter the mask pattern."""
        secret = "abcdefghijklmnop"
        masked = mask_secret(secret, head=1, tail=5)

        assert masked == secret[:4] + "***" + secret[-2:]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
