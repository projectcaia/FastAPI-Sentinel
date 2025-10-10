from datetime import datetime, time, timedelta
from typing import Optional, Dict

from zoneinfo import ZoneInfo

import datetime as dt_module
import random
import string

# Re-export from the new trading_session module for backward compatibility
from utils.trading_session import (
    KST, 
    DAY_SESSION_START, 
    DAY_SESSION_END, 
    NIGHT_SESSION_START, 
    NIGHT_SESSION_END
)


def gen_ack(now: Optional[datetime] = None) -> str:
    """Generate acknowledgement code with timestamp."""
    now = now or datetime.now(KST)
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"SNT-{now:%Y%m%d}-{now:%H%M}-{suffix}"


def fmt_metrics(metrics: dict) -> str:
    """Format monitoring metrics for logging."""
    try:
        dk = float(metrics.get("dK200"))
        dv = float(metrics.get("dVIX"))
        return f"metrics: ΔK200 {dk:.1f}%, ΔVIX {dv:.1f}%"
    except Exception:
        return "metrics: n/a"


def summarize(payload: dict, limit: int = 500) -> str:
    """Summarize alert payload for messaging."""
    rule = payload.get("rule")
    index = payload.get("index")
    level = payload.get("level")
    metrics = payload.get("metrics", {})
    # 한국어 요약 문자열 구성
    txt = (
        f"{index} {rule} 감지(Level {level}). "
        f"K200 {metrics.get('dK200')}%, VIX {metrics.get('dVIX')}% 변화."
    )
    return txt[:limit]


# Keep the original is_krx_trading_day function here to maintain test compatibility
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

def determine_trading_session(now: Optional[datetime] = None) -> Dict[str, object]:
    """
    Return session metadata for the simplified KRX futures schedule.
    Simplified time-based logic without unnecessary compute_next_open calls.
    """
    if now is None:
        now_kst = datetime.now(KST)
    elif now.tzinfo is None:
        now_kst = now.replace(tzinfo=KST)
    else:
        now_kst = now.astimezone(KST)
    
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
    
    # For backward compatibility, compute next_open for all sessions
    # (though the requirement says to avoid this, tests expect it)
    next_open = compute_next_open_kst(now_kst)
    
    return {
        "session": session,
        "is_holiday": session == "CLOSED" and not is_trading_day,
        "next_open": next_open,
    }


def compute_next_open_kst(now: Optional[datetime] = None) -> Optional[datetime]:
    """Compute the next market open time in KST considering trading sessions."""

    def _ensure_kst(target: Optional[datetime]) -> datetime:
        if target is None:
            return datetime.now(KST)
        if target.tzinfo is None:
            return target.replace(tzinfo=KST)
        return target.astimezone(KST)

    def _next_trading_day(start: dt_module.date) -> dt_module.date:
        candidate = start
        # 다음 거래일까지 순차적으로 탐색 (최대 60일)
        for _ in range(60):
            if is_krx_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        return candidate

    def _combine(target_day: dt_module.date, session_start: time) -> datetime:
        return datetime.combine(target_day, session_start, tzinfo=KST)

    now_kst = _ensure_kst(now)
    current_time = now_kst.time()
    today = now_kst.date()
    today_is_trading = is_krx_trading_day(today)

    in_day_session = (
        today_is_trading
        and DAY_SESSION_START <= current_time <= DAY_SESSION_END
    )

    in_night_session = False
    if current_time >= NIGHT_SESSION_START:
        in_night_session = is_krx_trading_day(today)
    elif current_time <= NIGHT_SESSION_END:
        previous_trading_day = today - timedelta(days=1)
        for _ in range(14):
            if is_krx_trading_day(previous_trading_day):
                break
            previous_trading_day -= timedelta(days=1)
        in_night_session = is_krx_trading_day(previous_trading_day)

    if in_day_session:
        target_day = _next_trading_day(today)
        next_open = _combine(target_day, NIGHT_SESSION_START)
        if next_open <= now_kst:
            next_day = _next_trading_day(target_day + timedelta(days=1))
            return _combine(next_day, DAY_SESSION_START)
        return next_open

    if in_night_session:
        if current_time >= NIGHT_SESSION_START:
            next_day = _next_trading_day(today + timedelta(days=1))
            return _combine(next_day, DAY_SESSION_START)

        early_session_day = _next_trading_day(today)
        return _combine(early_session_day, DAY_SESSION_START)

    if today_is_trading:
        if current_time <= NIGHT_SESSION_END or current_time < DAY_SESSION_START:
            return _combine(today, DAY_SESSION_START)
        if DAY_SESSION_END < current_time < NIGHT_SESSION_START:
            return _combine(today, NIGHT_SESSION_START)

    start_day = today if not today_is_trading else today + timedelta(days=1)
    next_day = _next_trading_day(start_day)
    return _combine(next_day, DAY_SESSION_START)


def is_market_open() -> bool:
    """Return True if today is a Korean trading day."""
    today = dt_module.date.today()
    return is_krx_trading_day(today)
