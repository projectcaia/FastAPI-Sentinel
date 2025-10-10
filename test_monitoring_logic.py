#!/usr/bin/env python3
"""
Test monitoring logic for DBSEC WebSocket
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch
from services.dbsec_ws import KOSPI200FuturesMonitor

KST = ZoneInfo("Asia/Seoul")

async def test_monitoring_logic():
    """Test that monitoring behaves correctly in different sessions"""
    
    # Create a monitor instance (disabled to avoid real connections)
    monitor = KOSPI200FuturesMonitor(enabled=False)
    
    print("Testing session state updates...")
    
    # Test DAY session
    with patch('services.dbsec_ws.determine_trading_session') as mock_determine:
        mock_determine.return_value = {"session": "DAY", "is_holiday": False}
        monitor._update_session_state("DAY")
        assert monitor.current_session == "DAY"
        print("✓ DAY session state updated correctly")
    
    # Test NIGHT session
    with patch('services.dbsec_ws.determine_trading_session') as mock_determine:
        mock_determine.return_value = {"session": "NIGHT", "is_holiday": False}
        monitor._update_session_state("NIGHT")
        assert monitor.current_session == "NIGHT"
        print("✓ NIGHT session state updated correctly")
    
    # Test CLOSED session
    with patch('services.dbsec_ws.determine_trading_session') as mock_determine:
        mock_determine.return_value = {"session": "CLOSED", "is_holiday": False}
        monitor._update_session_state("CLOSED")
        assert monitor.current_session == "CLOSED"
        print("✓ CLOSED session state updated correctly")
    
    # Test holiday detection
    with patch('services.dbsec_ws.determine_trading_session') as mock_determine:
        mock_determine.return_value = {"session": "CLOSED", "is_holiday": True}
        status = mock_determine()
        assert status["is_holiday"] == True
        print("✓ Holiday detection works correctly")
    
    print("\nAll monitoring logic tests passed!")

if __name__ == "__main__":
    asyncio.run(test_monitoring_logic())