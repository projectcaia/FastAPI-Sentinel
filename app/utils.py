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


def determine_trading_session(now: Optional[datetime] = None) -> str:
    """Return trading session name for simplified KRX futures schedule."""
    if now is None:
        now_kst = datetime.now(KST)
    elif now.tzinfo is None:
        now_kst = now.replace(tzinfo=KST)
    else:
        now_kst = now.astimezone(KST)

    current_time = now_kst.time()

    if DAY_SESSION_START <= current_time <= DAY_SESSION_END:
        return "DAY" if is_krx_trading_day(now_kst.date()) else "CLOSED"

    if current_time >= NIGHT_SESSION_START or current_time <= NIGHT_SESSION_END:
        reference_date = now_kst.date()
        if current_time <= NIGHT_SESSION_END:
            reference_date = reference_date - timedelta(days=1)

        return "NIGHT" if is_krx_trading_day(reference_date) else "CLOSED"

    return "CLOSED"


def is_market_open() -> bool:
    """Return True if today is a Korean trading day."""
    today = dt_module.date.today()
    return is_krx_trading_day(today)
