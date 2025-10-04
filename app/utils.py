from datetime import datetime
from zoneinfo import ZoneInfo
import random, string
def gen_ack(now=None) -> str:
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"SNT-{now:%Y%m%d}-{now:%H%M}-{suffix}"
def fmt_metrics(metrics: dict) -> str:
    try:
        dk = float(metrics.get("dK200")); dv = float(metrics.get("dVIX"))
        return f"metrics: ΔK200 {dk:.1f}%, ΔVIX {dv:.1f}%"
    except Exception:
        return "metrics: n/a"
def summarize(payload: dict, limit=500) -> str:
    rule = payload.get("rule"); index = payload.get("index"); level = payload.get("level")
    m = payload.get("metrics", {})
    txt = f"{index} {rule} 감지(Level {level}). K200 {m.get('dK200')}%, VIX {m.get('dVIX')}% 변화."
    return txt[:limit]


import datetime
try:
    import holidays
except ImportError:
    holidays = None


def is_krx_trading_day(day: datetime.date) -> bool:
    """Return True if the supplied day is a Korean trading day."""
    if holidays:
        kr_holidays = holidays.KR()
        return day.weekday() < 5 and day not in kr_holidays

    # holidays 패키지가 없으면 단순 주말 체크만
    return day.weekday() < 5


def is_market_open() -> bool:
    """Return True if today is a Korean trading day."""
    today = datetime.date.today()
    return is_krx_trading_day(today)
