"""
Unit tests for DB증권 module components
"""
import asyncio
import pytest
import json
import os
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone, timedelta

from utils.token_manager import DBSecTokenManager
from services.dbsec_ws import KOSPI200FuturesMonitor


class TestDBSecTokenManager:
    """Tests for DB증권 Token Manager"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.token_manager = DBSecTokenManager(
            app_key="test_key",
            app_secret="test_secret",
            base_url="https://test.api.com"
        )
    
    def test_token_manager_initialization(self):
        """Test token manager initialization"""
        assert self.token_manager.app_key == "test_key"
        assert self.token_manager.app_secret == "test_secret"
        assert self.token_manager.base_url == "https://test.api.com"
        assert self.token_manager.token_url == "https://test.api.com/oauth2/token"
        assert self.token_manager.access_token is None
        assert self.token_manager.expires_at is None
    
    def test_is_token_valid_no_token(self):
        """Test token validity check with no token"""
        assert not self.token_manager._is_token_valid()
    
    def test_is_token_valid_expired_token(self):
        """Test token validity check with expired token"""
        self.token_manager.access_token = "test_token"
        self.token_manager.expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        assert not self.token_manager._is_token_valid()
    
    def test_is_token_valid_valid_token(self):
        """Test token validity check with valid token"""
        self.token_manager.access_token = "test_token"
        self.token_manager.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        assert self.token_manager._is_token_valid()
    
    @pytest.mark.asyncio
    async def test_refresh_token_success(self):
        """Test successful token refresh"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_test_token",
            "expires_in": 3600,
            "token_type": "Bearer"
        }
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
            
            result = await self.token_manager._refresh_token()
            
            assert result is True
            assert self.token_manager.access_token == "new_test_token"
            assert self.token_manager.token_type == "Bearer"
            assert self.token_manager.expires_at is not None
    
    @pytest.mark.asyncio
    async def test_refresh_token_failure(self):
        """Test failed token refresh"""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
            
            result = await self.token_manager._refresh_token()
            
            assert result is False
            assert self.token_manager.access_token is None
    
    @pytest.mark.asyncio
    async def test_get_token_new_token(self):
        """Test getting token when no token exists"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_token",
            "expires_in": 3600,
            "token_type": "Bearer"
        }
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
            
            token = await self.token_manager.get_token()
            
            assert token == "new_token"
    
    def test_get_auth_header_no_token(self):
        """Test auth header with no token"""
        headers = self.token_manager.get_auth_header()
        assert headers == {}
    
    def test_get_auth_header_with_token(self):
        """Test auth header with token"""
        self.token_manager.access_token = "test_token"
        self.token_manager.token_type = "Bearer"
        
        headers = self.token_manager.get_auth_header()
        assert headers == {"Authorization": "Bearer test_token"}
    
    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test health check method"""
        self.token_manager.access_token = "test_token"
        self.token_manager.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        
        health = await self.token_manager.health_check()
        
        assert health["token_valid"] is True
        assert health["has_token"] is True
        assert health["expires_at"] is not None
        assert health["refresh_task_active"] is False


class TestKOSPI200FuturesMonitor:
    """Tests for KOSPI200 Futures Monitor"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.monitor = KOSPI200FuturesMonitor(
            alert_threshold=1.0,
            buffer_size=10,
            ws_url="wss://test.ws.com"
        )
    
    def test_monitor_initialization(self):
        """Test monitor initialization"""
        assert self.monitor.alert_threshold == 1.0
        assert self.monitor.buffer_size == 10
        assert self.monitor.ws_url == "wss://test.ws.com"
        assert len(self.monitor.tick_buffer) == 0
        assert self.monitor.last_price is None
        assert not self.monitor.is_connected
    
    def test_determine_session_day(self):
        """Test session determination for day session"""
        import pytz
        from datetime import datetime, time
        
        with patch('services.dbsec_ws.datetime') as mock_datetime:
            # Mock 10:00 KST (day session)
            kst = pytz.timezone('Asia/Seoul')
            mock_time = datetime(2024, 1, 1, 10, 0, 0).replace(tzinfo=kst)
            mock_datetime.now.return_value = mock_time
            mock_datetime.time = time
            
            session = self.monitor._determine_session()
            assert session == "DAY"
    
    def test_determine_session_night(self):
        """Test session determination for night session"""
        import pytz
        from datetime import datetime, time
        
        with patch('services.dbsec_ws.datetime') as mock_datetime:
            # Mock 20:00 KST (night session)
            kst = pytz.timezone('Asia/Seoul')
            mock_time = datetime(2024, 1, 1, 20, 0, 0).replace(tzinfo=kst)
            mock_datetime.now.return_value = mock_time
            mock_datetime.time = time
            
            session = self.monitor._determine_session()
            assert session == "NIGHT"
    
    @pytest.mark.asyncio
    async def test_parse_tick_data_valid(self):
        """Test parsing valid tick data"""
        raw_data = {
            "body": {
                "stck_prpr": "350.50",
                "cntg_vol": "1000"
            }
        }
        
        with patch.object(self.monitor, '_determine_session', return_value="DAY"):
            tick = await self.monitor._parse_tick_data(raw_data)
            
            assert tick is not None
            assert tick["symbol"] == "K200_FUT"
            assert tick["price"] == 350.50
            assert tick["volume"] == 1000
            assert tick["session"] == "DAY"
    
    @pytest.mark.asyncio
    async def test_parse_tick_data_invalid_price(self):
        """Test parsing tick data with invalid price"""
        raw_data = {
            "body": {
                "stck_prpr": "0",
                "cntg_vol": "1000"
            }
        }
        
        tick = await self.monitor._parse_tick_data(raw_data)
        assert tick is None
    
    @pytest.mark.asyncio
    async def test_check_anomaly_no_alert(self):
        """Test anomaly check with no alert threshold breach"""
        tick = {
            "symbol": "K200_FUT",
            "session": "DAY",
            "change_rate": 0.5,  # Below 1.0% threshold
            "price": 350.0,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        with patch.object(self.monitor, '_send_to_caia_agent') as mock_send:
            await self.monitor._check_anomaly(tick)
            mock_send.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_check_anomaly_with_alert(self):
        """Test anomaly check with alert threshold breach"""
        tick = {
            "symbol": "K200_FUT",
            "session": "DAY",
            "change_rate": 1.5,  # Above 1.0% threshold
            "price": 350.0,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        with patch.object(self.monitor, '_send_to_caia_agent') as mock_send:
            await self.monitor._check_anomaly(tick)
            mock_send.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_to_caia_agent_success(self):
        """Test successful Caia Agent notification"""
        payload = {
            "symbol": "K200_FUT",
            "session": "DAY",
            "change": 1.5,
            "price": 350.0,
            "timestamp": "2024-01-01T12:00:00Z"
        }
        
        # Set CAIA_AGENT_URL for test
        self.monitor.caia_agent_url = "https://test-agent.com"
        
        mock_response = Mock()
        mock_response.status_code = 200
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
            
            await self.monitor._send_to_caia_agent(payload)
            
            # Verify the call was made
            mock_client.return_value.__aenter__.return_value.post.assert_called_once()
    
    def test_get_recent_ticks_empty(self):
        """Test getting recent ticks from empty buffer"""
        ticks = self.monitor.get_recent_ticks()
        assert len(ticks) == 0
    
    def test_get_recent_ticks_with_data(self):
        """Test getting recent ticks with data"""
        # Add some test data to buffer
        test_tick = {
            "timestamp": "2024-01-01T12:00:00Z",
            "symbol": "K200_FUT",
            "price": 350.0,
            "volume": 1000,
            "session": "DAY",
            "change_rate": 0.5
        }
        self.monitor.tick_buffer.append(test_tick)
        
        ticks = self.monitor.get_recent_ticks()
        assert len(ticks) == 1
        assert ticks[0] == test_tick
    
    def test_get_health_status(self):
        """Test health status reporting"""
        health = self.monitor.get_health_status()
        
        assert "connected" in health
        assert "reconnect_attempts" in health
        assert "buffer_size" in health
        assert "alert_threshold" in health
        assert health["connected"] is False
        assert health["alert_threshold"] == 1.0


class TestIntegration:
    """Integration tests for the complete DB증권 module"""
    
    @pytest.mark.asyncio
    async def test_token_manager_websocket_integration(self):
        """Test integration between token manager and WebSocket client"""
        # Mock environment variables
        with patch.dict(os.environ, {
            'DB_APP_KEY': 'test_key',
            'DB_APP_SECRET': 'test_secret',
            'CAIA_AGENT_URL': 'https://test-agent.com'
        }):
            from utils.token_manager import get_token_manager
            from services.dbsec_ws import get_futures_monitor
            
            # Get instances
            token_manager = get_token_manager()
            monitor = get_futures_monitor()
            
            assert token_manager is not None
            assert monitor is not None
            assert token_manager.app_key == 'test_key'
            assert monitor.caia_agent_url == 'https://test-agent.com'
    
    def test_environment_configuration(self):
        """Test environment variable configuration"""
        test_env = {
            'DB_APP_KEY': 'test_app_key',
            'DB_APP_SECRET': 'test_secret',
            'DB_API_BASE': 'https://custom.api.com',
            'DB_ALERT_THRESHOLD': '2.0',
            'DB_BUFFER_SIZE': '200',
            'CAIA_AGENT_URL': 'https://caia.test.com'
        }
        
        with patch.dict(os.environ, test_env):
            from utils.token_manager import get_token_manager
            from services.dbsec_ws import get_futures_monitor
            
            token_manager = get_token_manager()
            monitor = get_futures_monitor()
            
            assert token_manager.app_key == 'test_app_key'
            assert token_manager.base_url == 'https://custom.api.com'
            assert monitor.alert_threshold == 2.0
            assert monitor.buffer_size == 200
            assert monitor.caia_agent_url == 'https://caia.test.com'


# Run tests with: pytest tests/test_dbsec_module.py -v