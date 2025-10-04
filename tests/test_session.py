"""Validation tests for trading session utilities."""
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Optional

import pytest
from zoneinfo import ZoneInfo

from app import utils
from services import dbsec_ws

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture()
def trading_calendar(monkeypatch) -> Callable[[Iterable[date]], None]:
    """Patch the KRX calendar with a deterministic active-day set."""

    active_days = set()

    def configure(days: Iterable[date]) -> None:
        active_days.clear()
        active_days.update(days)

    def fake_is_trading(target: date) -> bool:
        return target in active_days

    monkeypatch.setattr(utils, "is_krx_trading_day", fake_is_trading)
    return configure


class TestDetermineTradingSession:
    """Verify determine_trading_session across key schedule boundaries."""

    def test_day_session_on_trading_day(self, monkeypatch, trading_calendar):
        """A trading morning should return a DAY session with next open value."""

        trading_calendar({date(2024, 1, 2)})
        sentinel_next = datetime(2024, 1, 2, 18, 0, tzinfo=KST)
        monkeypatch.setattr(
            utils,
            "compute_next_open_kst",
            lambda *_args, **_kwargs: sentinel_next,
        )

        now = datetime(2024, 1, 2, 9, 30, tzinfo=KST)
        status = utils.determine_trading_session(now)

        assert status["session"] == "DAY"
        assert status["is_holiday"] is False
        assert status["next_open"] == sentinel_next

    def test_evening_night_session(self, monkeypatch, trading_calendar):
        """Evening trading hours should map to the NIGHT session."""

        trading_calendar({date(2024, 1, 2)})
        sentinel_next = datetime(2024, 1, 3, 9, 0, tzinfo=KST)
        monkeypatch.setattr(
            utils,
            "compute_next_open_kst",
            lambda *_args, **_kwargs: sentinel_next,
        )

        now = datetime(2024, 1, 2, 18, 15, tzinfo=KST)
        status = utils.determine_trading_session(now)

        assert status["session"] == "NIGHT"
        assert status["is_holiday"] is False
        assert status["next_open"] == sentinel_next

    def test_post_midnight_refers_to_prior_trading_day(self, monkeypatch, trading_calendar):
        """Post-midnight trading should still reference the prior trading day."""

        trading_calendar({date(2024, 1, 2)})
        sentinel_next = datetime(2024, 1, 3, 9, 0, tzinfo=KST)
        monkeypatch.setattr(
            utils,
            "compute_next_open_kst",
            lambda *_args, **_kwargs: sentinel_next,
        )

        now = datetime(2024, 1, 3, 1, 30, tzinfo=KST)
        status = utils.determine_trading_session(now)

        assert status["session"] == "NIGHT"
        assert status["is_holiday"] is False
        assert status["next_open"] == sentinel_next

    def test_holiday_during_session_hours_marks_closed(self, monkeypatch, trading_calendar):
        """Holiday hours within the trading window should be flagged as closed."""

        trading_calendar({date(2024, 1, 2)})
        sentinel_next = datetime(2024, 1, 2, 18, 0, tzinfo=KST)
        monkeypatch.setattr(
            utils,
            "compute_next_open_kst",
            lambda *_args, **_kwargs: sentinel_next,
        )

        now = datetime(2024, 1, 1, 10, 0, tzinfo=KST)
        status = utils.determine_trading_session(now)

        assert status["session"] == "CLOSED"
        assert status["is_holiday"] is True
        assert status["next_open"] == sentinel_next

    def test_weekend_reports_closed(self, monkeypatch, trading_calendar):
        """Weekends should be treated as closed periods."""

        trading_calendar({date(2024, 1, 5)})
        sentinel_next = datetime(2024, 1, 8, 9, 0, tzinfo=KST)
        monkeypatch.setattr(
            utils,
            "compute_next_open_kst",
            lambda *_args, **_kwargs: sentinel_next,
        )

        now = datetime(2024, 1, 6, 11, 0, tzinfo=KST)
        status = utils.determine_trading_session(now)

        assert status["session"] == "CLOSED"
        assert status["is_holiday"] is True
        assert status["next_open"] == sentinel_next


class TestComputeNextOpen:
    """Validate compute_next_open_kst scheduling across trading states."""

    def test_day_session_returns_same_day_night_open(self, monkeypatch, trading_calendar):
        """Daytime trading should schedule the upcoming night session."""

        trading_calendar({date(2024, 1, 2), date(2024, 1, 3)})
        monkeypatch.setattr(utils, "determine_trading_session", lambda *_: "DAY")

        now = datetime(2024, 1, 2, 13, 0, tzinfo=KST)
        expected = datetime(2024, 1, 2, 18, 0, tzinfo=KST)

        assert utils.compute_next_open_kst(now) == expected

    def test_night_session_after_open_targets_next_day_morning(self, monkeypatch, trading_calendar):
        """Night trading after 18:00 should roll forward to the next morning open."""

        trading_calendar({date(2024, 1, 2), date(2024, 1, 3)})
        monkeypatch.setattr(utils, "determine_trading_session", lambda *_: "NIGHT")

        now = datetime(2024, 1, 2, 19, 30, tzinfo=KST)
        expected = datetime(2024, 1, 3, 9, 0, tzinfo=KST)

        assert utils.compute_next_open_kst(now) == expected

    def test_night_session_before_morning_targets_same_day_open(self, monkeypatch, trading_calendar):
        """Night session before sunrise should target the same day's morning open."""

        trading_calendar({date(2024, 1, 3), date(2024, 1, 4)})
        monkeypatch.setattr(utils, "determine_trading_session", lambda *_: "NIGHT")

        now = datetime(2024, 1, 3, 1, 0, tzinfo=KST)
        expected = datetime(2024, 1, 3, 9, 0, tzinfo=KST)

        assert utils.compute_next_open_kst(now) == expected

    def test_pre_market_closed_returns_same_day_open(self, monkeypatch, trading_calendar):
        """Pre-market hours on a trading day should return the upcoming morning open."""

        trading_calendar({date(2024, 1, 4)})
        monkeypatch.setattr(utils, "determine_trading_session", lambda *_: "CLOSED")

        now = datetime(2024, 1, 4, 8, 30, tzinfo=KST)
        expected = datetime(2024, 1, 4, 9, 0, tzinfo=KST)

        assert utils.compute_next_open_kst(now) == expected

    def test_afternoon_lull_points_to_night_session(self, monkeypatch, trading_calendar):
        """Afternoon gaps before the night session should point to the evening open."""

        trading_calendar({date(2024, 1, 4)})
        monkeypatch.setattr(utils, "determine_trading_session", lambda *_: "CLOSED")

        now = datetime(2024, 1, 4, 15, 45, tzinfo=KST)
        expected = datetime(2024, 1, 4, 18, 0, tzinfo=KST)

        assert utils.compute_next_open_kst(now) == expected

    def test_non_trading_day_advances_to_next_available(self, monkeypatch, trading_calendar):
        """Closed holidays should advance to the next available trading morning."""

        trading_calendar({date(2024, 1, 8)})
        monkeypatch.setattr(utils, "determine_trading_session", lambda *_: "CLOSED")

        now = datetime(2024, 1, 6, 10, 0, tzinfo=KST)
        expected = datetime(2024, 1, 8, 9, 0, tzinfo=KST)

        assert utils.compute_next_open_kst(now) == expected


@pytest.fixture()
def fixed_datetime(monkeypatch):
    """Provide a controllable datetime.now implementation for sleep tests."""

    class FixedDateTime(datetime):
        fixed_now: datetime = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz: Optional[timezone] = None) -> datetime:
            value = cls.fixed_now
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    monkeypatch.setattr(dbsec_ws, "datetime", FixedDateTime)
    return FixedDateTime


class TestSleepUntil:
    """Validate sleep_until timing logic including capping and guard rails."""

    @pytest.mark.asyncio()
    async def test_sleep_until_future_without_cap(self, monkeypatch, fixed_datetime):
        """Ensure sleep_until waits for the full interval when uncapped."""

        captured = {}

        async def fake_sleep(delay: float):
            captured["delay"] = delay

        monkeypatch.setattr(dbsec_ws.asyncio, "sleep", fake_sleep)

        fixed_datetime.fixed_now = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        target = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)

        await dbsec_ws.sleep_until(target)

        assert captured["delay"] == pytest.approx(300.0)

    @pytest.mark.asyncio()
    async def test_sleep_until_applies_cap(self, monkeypatch, fixed_datetime):
        """Ensure sleep_until honors the optional maximum cap."""

        captured = {}

        async def fake_sleep(delay: float):
            captured["delay"] = delay

        monkeypatch.setattr(dbsec_ws.asyncio, "sleep", fake_sleep)

        fixed_datetime.fixed_now = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        target = datetime(2024, 1, 1, 20, 0, tzinfo=timezone.utc)

        await dbsec_ws.sleep_until(target, max_cap_hours=1)

        assert captured["delay"] == pytest.approx(3600.0)

    @pytest.mark.asyncio()
    async def test_sleep_until_no_wait_when_target_passed(self, monkeypatch, fixed_datetime):
        """Ensure elapsed targets bypass sleeping entirely."""

        called = False

        async def fake_sleep(delay: float):  # pragma: no cover - should not run
            nonlocal called
            called = True

        monkeypatch.setattr(dbsec_ws.asyncio, "sleep", fake_sleep)

        fixed_datetime.fixed_now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        target = datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc)

        await dbsec_ws.sleep_until(target)

        assert called is False
