#!/usr/bin/env python3
"""
Manual test for trading session determination
Tests the key time points mentioned in the requirements
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from app.utils import determine_trading_session
from utils.trading_session import determine_trading_session as simple_determine_trading_session

KST = ZoneInfo("Asia/Seoul")

# Test cases for manual verification
test_cases = [
    ("09:00 - Start of day session", datetime(2024, 1, 8, 9, 0, tzinfo=KST)),  # Monday
    ("15:29 - Near end of day session", datetime(2024, 1, 8, 15, 29, tzinfo=KST)),
    ("15:31 - After day session", datetime(2024, 1, 8, 15, 31, tzinfo=KST)),
    ("18:05 - Start of night session", datetime(2024, 1, 8, 18, 5, tzinfo=KST)),
    ("04:59 - Near end of night session", datetime(2024, 1, 9, 4, 59, tzinfo=KST)),  # Tuesday early morning
    ("12:00 - Mid day on weekend", datetime(2024, 1, 6, 12, 0, tzinfo=KST)),  # Saturday
]

print("=" * 80)
print("Trading Session Determination Test")
print("=" * 80)

for description, test_time in test_cases:
    print(f"\nTest: {description}")
    print(f"Time: {test_time.strftime('%Y-%m-%d %H:%M %Z')} ({test_time.strftime('%A')})")
    
    # Test app.utils version (backward compatible)
    result_app = determine_trading_session(test_time)
    print(f"app.utils result:")
    print(f"  Session: {result_app['session']}")
    print(f"  Is Holiday: {result_app['is_holiday']}")
    if result_app.get('next_open'):
        print(f"  Next Open: {result_app['next_open'].strftime('%Y-%m-%d %H:%M %Z')}")
    
    # Test new simplified version
    result_simple = simple_determine_trading_session(test_time)
    print(f"utils.trading_session result:")
    print(f"  Session: {result_simple['session']}")
    print(f"  Is Holiday: {result_simple['is_holiday']}")
    
    # Verify consistency
    assert result_app['session'] == result_simple['session'], f"Session mismatch at {test_time}"
    print("  ✓ Results consistent")

print("\n" + "=" * 80)
print("All manual tests passed!")
print("=" * 80)