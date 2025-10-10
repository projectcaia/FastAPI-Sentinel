"""
Trading session determination module.
Simplified logic for determining KRX trading sessions based on time only.
"""
from datetime import datetime, time, timedelta
from typing import Optional, Dict
from zoneinfo import ZoneInfo
import datetime as dt_module

KST = ZoneInfo("Asia/Seoul")
DAY_SESSION_START = time(9, 0)
DAY_SESSION_END = time(15, 30)
NIGHT_SESSION_START = time(18, 0)
NIGHT_SESSION_END = time(5, 0)

try:
    import holidays
except ImportError:  # pragma: no cover
    holidays = None


def is_krx_trading_day(day: dt_module.date) -> bool:
    """Return True if the supplied day is a Korean trading day."""
    if holidays:
        kr_holidays = holidays.KR()
        return day.weekday() < 5 and day not in kr_holidays
    
    # holidays 패키지가 없으면 단순 주말 체크만
    return day.weekday() < 5


def determine_trading_session(now_kst: Optional[datetime] = None) -> Dict[str, object]:
    """
    Determine current trading session based on time.
    
    Simplified logic:
    - Check if today is a trading day once using is_krx_trading_day()
    - DAY session: 09:00 - 15:30
    - NIGHT session: 18:00 - 05:00 (next day)
    - CLOSED: All other times or holidays
    
    Returns:
        dict: {
            "session": "DAY" | "NIGHT" | "CLOSED",
            "is_holiday": bool (True if it's a holiday/weekend)
        }
    """
    if now_kst is None:
        now_kst = datetime.now(KST)
    elif now_kst.tzinfo is None:
        now_kst = now_kst.replace(tzinfo=KST)
    else:
        now_kst = now_kst.astimezone(KST)
    
    current_time = now_kst.time()
    today = now_kst.date()
    
    # Single check for trading day
    is_trading_day = is_krx_trading_day(today)
    
    # For night session that spans midnight, check the reference day
    # If current time is after midnight (00:00-05:00), reference day is yesterday
    if current_time <= NIGHT_SESSION_END:
        reference_day = today - timedelta(days=1)
        is_reference_trading_day = is_krx_trading_day(reference_day)
    else:
        is_reference_trading_day = is_trading_day
    
    # Determine session based on time - only if it's a trading day
    session = "CLOSED"
    
    if DAY_SESSION_START <= current_time <= DAY_SESSION_END:
        if is_trading_day:
            session = "DAY"
    elif current_time >= NIGHT_SESSION_START or current_time <= NIGHT_SESSION_END:
        if is_reference_trading_day:
            session = "NIGHT"
    
    # Determine if it's a holiday (non-trading day)
    is_holiday = not is_trading_day
    
    return {
        "session": session,
        "is_holiday": is_holiday
    }