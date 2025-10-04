from datetime import datetime, time, timedelta
from typing import Optional

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


def _ensure_kst(now: Optional[datetime]) -> datetime:
    """Normalize any datetime to a timezone-aware KST value."""
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


def determine_trading_session(now: Optional[datetime] = None) -> str:
    """Return the active trading session label for the supplied timestamp."""
    now_kst = _ensure_kst(now)
    current_time = now_kst.time()
    today = now_kst.date()

    if DAY_SESSION_START <= current_time <= DAY_SESSION_END:
        if is_krx_trading_day(today):
            return "DAY"
        return "CLOSED"

    if current_time >= NIGHT_SESSION_START:
        return "NIGHT" if is_krx_trading_day(today) else "CLOSED"

    if current_time <= NIGHT_SESSION_END:
        previous_day = today - timedelta(days=1)
        return "NIGHT" if is_krx_trading_day(previous_day) else "CLOSED"

    return "CLOSED"


def _next_trading_day(start_day: dt_module.date, include_start: bool = False) -> dt_module.date:
    """Return the next trading day on or after the supplied date."""
    candidate = start_day if include_start else start_day + timedelta(days=1)

    # 다음 거래일까지 순차적으로 탐색
    while not is_krx_trading_day(candidate):
        candidate += timedelta(days=1)

    return candidate


def compute_next_open_kst(now: Optional[datetime] = None) -> datetime:
    """Compute the next trading session opening time in KST."""
    now_kst = _ensure_kst(now)
    current_time = now_kst.time()
    today = now_kst.date()
    session = determine_trading_session(now_kst)
    today_is_trading = is_krx_trading_day(today)

    def _combine(day: dt_module.date, session_start: time) -> datetime:
        return datetime.combine(day, session_start, tzinfo=KST)

    if session == "DAY":
        if today_is_trading:
            night_open = _combine(today, NIGHT_SESSION_START)
            if night_open > now_kst:
                return night_open
        next_day = _next_trading_day(today)
        return _combine(next_day, DAY_SESSION_START)

    if session == "NIGHT":
        if current_time >= NIGHT_SESSION_START:
            next_day = _next_trading_day(today)
            return _combine(next_day, DAY_SESSION_START)

        if today_is_trading:
            return _combine(today, DAY_SESSION_START)

        next_day = _next_trading_day(today, include_start=True)
        return _combine(next_day, DAY_SESSION_START)

    if today_is_trading:
        if current_time < DAY_SESSION_START or current_time <= NIGHT_SESSION_END:
            return _combine(today, DAY_SESSION_START)
        if DAY_SESSION_END < current_time < NIGHT_SESSION_START:
            return _combine(today, NIGHT_SESSION_START)

    if current_time <= NIGHT_SESSION_END:
        next_day = _next_trading_day(today, include_start=True)
        return _combine(next_day, DAY_SESSION_START)

    next_day = _next_trading_day(today, include_start=not today_is_trading)
    return _combine(next_day, DAY_SESSION_START)


def is_market_open() -> bool:
    """Return True if today is a Korean trading day."""
    today = dt_module.date.today()
    return is_krx_trading_day(today)
