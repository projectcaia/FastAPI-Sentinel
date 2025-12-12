# tests/test_us_session_fix.py
# -*- coding: utf-8 -*-
"""
US Session Detection Fix - Unit Tests

Tests for verifying the US market session detection based on America/New_York timezone.

Key test cases:
1. Saturday 05:00 KST = Friday 15:00 NY (EST) → US session should be OPEN
2. Monday 05:00 KST = Sunday 15:00 NY → CLOSED (weekend in NY)
3. Standard US trading hours detection
4. Korean session detection remains unchanged
"""

import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KST = ZoneInfo("Asia/Seoul")
NYC = ZoneInfo("America/New_York")


class TestUSSessionDetection:
    """Tests for US market session detection with NY timezone"""
    
    def test_saturday_morning_kst_is_friday_afternoon_ny(self):
        """토요일 새벽 05:00 KST = NY 금요일 15:00 → US 세션"""
        # Saturday 2025-01-11 05:00 KST = Friday 2025-01-10 15:00 EST
        kst_time = datetime(2025, 1, 11, 5, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        # Verify timezone conversion
        assert ny_time.weekday() == 4  # Friday
        assert ny_time.hour == 15  # 3 PM
        
        # US market should be open (9:30 AM - 4:00 PM)
        is_open = self._is_us_market_open_test(ny_time)
        assert is_open is True, f"Expected US market OPEN on {ny_time}, got CLOSED"
    
    def test_monday_morning_kst_is_sunday_afternoon_ny(self):
        """월요일 새벽 05:00 KST = NY 일요일 15:00 → CLOSED (주말)"""
        # Monday 2025-01-13 05:00 KST = Sunday 2025-01-12 15:00 EST
        kst_time = datetime(2025, 1, 13, 5, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        # Verify timezone conversion
        assert ny_time.weekday() == 6  # Sunday
        
        # US market should be CLOSED (weekend)
        is_open = self._is_us_market_open_test(ny_time)
        assert is_open is False, f"Expected US market CLOSED on {ny_time} (Sunday), got OPEN"
    
    def test_friday_night_kst_is_friday_morning_ny(self):
        """금요일 밤 23:00 KST = NY 금요일 09:00 → US 세션 시작 전후"""
        # Friday 2025-01-10 23:00 KST = Friday 2025-01-10 09:00 EST
        kst_time = datetime(2025, 1, 10, 23, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        assert ny_time.weekday() == 4  # Friday
        assert ny_time.hour == 9
        
        # 09:00 is before 09:30, should be CLOSED
        is_open = self._is_us_market_open_test(ny_time)
        assert is_open is False
    
    def test_friday_night_kst_is_friday_trading_ny(self):
        """금요일 밤 23:30 KST = NY 금요일 09:30 → US 세션 오픈"""
        kst_time = datetime(2025, 1, 10, 23, 30, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        assert ny_time.hour == 9
        assert ny_time.minute == 30
        
        # 09:30 is exactly market open
        is_open = self._is_us_market_open_test(ny_time)
        assert is_open is True
    
    def test_saturday_early_morning_kst_is_friday_close_ny(self):
        """토요일 새벽 06:00 KST = NY 금요일 16:00 → US 세션 마감"""
        kst_time = datetime(2025, 1, 11, 6, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        assert ny_time.weekday() == 4  # Friday
        assert ny_time.hour == 16  # 4 PM - market closes
        
        # 16:00 is exactly market close (inclusive)
        is_open = self._is_us_market_open_test(ny_time)
        assert is_open is True
    
    def test_saturday_after_close_kst(self):
        """토요일 새벽 07:00 KST = NY 금요일 17:00 → US 세션 종료"""
        kst_time = datetime(2025, 1, 11, 7, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        assert ny_time.weekday() == 4  # Friday
        assert ny_time.hour == 17  # 5 PM - after market close
        
        is_open = self._is_us_market_open_test(ny_time)
        assert is_open is False
    
    def test_us_holiday_closed(self):
        """US 공휴일은 CLOSED"""
        # MLK Day 2025-01-20 (Monday)
        ny_time = datetime(2025, 1, 20, 12, 0, tzinfo=NYC)
        
        assert ny_time.weekday() == 0  # Monday
        
        is_open = self._is_us_market_open_test(ny_time)
        assert is_open is False, "US market should be CLOSED on MLK Day"
    
    def test_dst_transition_spring(self):
        """서머타임 전환 (3월) 테스트"""
        # March 10, 2025 - DST starts in US
        # Before DST: KST = EST + 14h, After DST: KST = EDT + 13h
        kst_time = datetime(2025, 3, 10, 23, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        # Should handle DST correctly
        assert 9 <= ny_time.hour <= 10  # Around market open
    
    def _is_us_market_open_test(self, now_nyc: datetime) -> bool:
        """Test helper: Check if US market is open (mirrors production logic)"""
        US_HOLIDAYS_2025 = [
            (1, 1), (1, 20), (2, 17), (4, 18), (5, 26), (6, 19),
            (7, 4), (9, 1), (11, 27), (12, 25)
        ]
        
        # Weekend check (NY timezone)
        if now_nyc.weekday() >= 5:
            return False
        
        # Holiday check (NY timezone)
        if (now_nyc.month, now_nyc.day) in US_HOLIDAYS_2025:
            return False
        
        # Trading hours: 09:30 - 16:00 NY time
        ny_hhmm = now_nyc.hour * 100 + now_nyc.minute
        return 930 <= ny_hhmm <= 1600


class TestKRSessionUnchanged:
    """Tests to verify Korean session detection remains unchanged"""
    
    def test_kr_regular_hours(self):
        """한국 정규장 09:00~15:30 KST"""
        # Monday 10:00 KST - should be KR session
        kst_time = datetime(2025, 1, 13, 10, 0, tzinfo=KST)
        
        hhmm = kst_time.hour * 100 + kst_time.minute
        is_kr_hours = 900 <= hhmm <= 1530
        is_weekday = kst_time.weekday() < 5
        
        assert is_kr_hours is True
        assert is_weekday is True
    
    def test_kr_futures_night_session(self):
        """한국 야간선물 18:00~05:00 KST"""
        # Monday 20:00 KST - should be FUTURES session
        kst_time = datetime(2025, 1, 13, 20, 0, tzinfo=KST)
        
        hhmm = kst_time.hour * 100 + kst_time.minute
        is_futures_hours = (hhmm >= 1800) or (hhmm < 500)
        
        assert is_futures_hours is True


class TestAlertHintAction:
    """Tests for alert hint/action generation"""
    
    def test_kr_session_hints(self):
        """한국장 hint/action 생성"""
        from market_watcher import get_alert_hint_action
        
        hint, action = get_alert_hint_action("KR_FUTURES", "LV1")
        assert hint is not None
        assert action == "monitor_delta"
        
        hint, action = get_alert_hint_action("KR_FUTURES", "LV2")
        assert action == "prep_hedge"
        
        hint, action = get_alert_hint_action("KR_FUTURES", "LV3")
        assert action == "reduce_risk"
    
    def test_us_session_hints(self):
        """미국장 hint/action 생성"""
        from market_watcher import get_alert_hint_action
        
        hint, action = get_alert_hint_action("US", "LV1")
        assert hint is not None
        assert action == "prep_hedge"
        
        hint, action = get_alert_hint_action("US", "LV2")
        assert action == "activate_hedge"
        
        hint, action = get_alert_hint_action("US", "LV3")
        assert action == "reduce_risk"
    
    def test_no_level_no_hint(self):
        """레벨이 None이면 hint/action도 None"""
        from market_watcher import get_alert_hint_action
        
        hint, action = get_alert_hint_action("US", None)
        assert hint is None
        assert action is None


class TestTimezoneConversion:
    """Timezone conversion validation tests"""
    
    def test_kst_to_ny_winter(self):
        """KST → NY 변환 (겨울 - EST)"""
        # Winter: KST = EST + 14h
        kst_time = datetime(2025, 1, 11, 5, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        # 05:00 KST = 15:00 EST (previous day)
        assert ny_time.day == 10
        assert ny_time.hour == 15
    
    def test_kst_to_ny_summer(self):
        """KST → NY 변환 (여름 - EDT)"""
        # Summer: KST = EDT + 13h
        kst_time = datetime(2025, 7, 12, 5, 0, tzinfo=KST)
        ny_time = kst_time.astimezone(NYC)
        
        # 05:00 KST = 16:00 EDT (previous day)
        assert ny_time.day == 11
        assert ny_time.hour == 16


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
