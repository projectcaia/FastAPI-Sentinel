from datetime import datetime, time, timedelta
from typing import Optional, Dict

from zoneinfo import ZoneInfo

import datetime as dt_module
import random
import string


KST = ZoneInfo("Asia/Seoul")
DAY_SESSION_START = time(9, 0)
DAY_SESSION_END = time(15, 30)
NIGHT_SESSION_START = time(18, 0)
NIGHT_SESSION_END = time(5, 0)


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


try:
    import holidays
except ImportError:  # pragma: no cover - optional dependency
    holidays = None


def is_krx_trading_day(day: dt_module.date) -> bool:
    """Return True if the supplied day is a Korean trading day."""
    if holidays:
        kr_holidays = holidays.KR()
        return day.weekday() < 5 and day not in kr_holidays

    # holidays 패키지가 없으면 단순 주말 체크만
    return day.weekday() < 5


def determine_trading_session(now: Optional[datetime] = None) -> Dict[str, object]:
    """Return session metadata for the simplified KRX futures schedule."""
    if now is None:
        now_kst = datetime.now(KST)
    elif now.tzinfo is None:
        now_kst = now.replace(tzinfo=KST)
    else:
        now_kst = now.astimezone(KST)

    current_time = now_kst.time()
    session = "CLOSED"
    is_holiday = False

    if DAY_SESSION_START <= current_time <= DAY_SESSION_END:
        if is_krx_trading_day(now_kst.date()):
            session = "DAY"
        else:
            is_holiday = True
    elif current_time >= NIGHT_SESSION_START or current_time <= NIGHT_SESSION_END:
        reference_date = now_kst.date()
        if current_time <= NIGHT_SESSION_END:
            reference_date = reference_date - timedelta(days=1)

        if is_krx_trading_day(reference_date):
            session = "NIGHT"
        else:
            is_holiday = True
    else:
        if not is_krx_trading_day(now_kst.date()):
            is_holiday = True

    next_open = compute_next_open_kst(now_kst)

    return {
        "session": session,
        "is_holiday": bool(session == "CLOSED" and is_holiday),
        "next_open": next_open,
    }


def compute_next_open_kst(now: Optional[datetime] = None) -> datetime:
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
