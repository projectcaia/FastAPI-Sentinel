"""Validation tests for trading session utilities."""
from datetime import date, datetime

import pytest
from zoneinfo import ZoneInfo

from app import utils

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def trading_calendar(monkeypatch):
    """Patch the trading calendar to use a deterministic set of active days."""
    active_days = set()

    def set_days(days):
        active_days.clear()
        active_days.update(days)

    def fake_is_trading(day):
        return day in active_days

    monkeypatch.setattr(utils, "is_krx_trading_day", fake_is_trading)
    return set_days


class TestDetermineTradingSession:
    """Verify determine_trading_session across session boundaries."""

    def test_day_session_on_trading_day(self, trading_calendar):
        """Ensure a trading day morning falls into the DAY session."""
        trading_calendar({date(2024, 1, 2)})
        now = datetime(2024, 1, 2, 9, 0, tzinfo=KST)

        result = utils.determine_trading_session(now)
        assert result["session"] == "DAY"

    def test_evening_night_session(self, trading_calendar):
        """Ensure evening times shift into the NIGHT session."""
        trading_calendar({date(2024, 1, 2)})
        now = datetime(2024, 1, 2, 18, 30, tzinfo=KST)

        result = utils.determine_trading_session(now)
        assert result["session"] == "NIGHT"

    def test_early_morning_night_session(self, trading_calendar):
        """Ensure post-midnight times still map to NIGHT for prior trading days."""
        trading_calendar({date(2024, 1, 2)})
        now = datetime(2024, 1, 3, 2, 0, tzinfo=KST)

        result = utils.determine_trading_session(now)
        assert result["session"] == "NIGHT"

    def test_closed_on_weekend(self, trading_calendar):
        """Ensure weekends are treated as closed regardless of time."""
        trading_calendar({date(2024, 1, 5)})
        now = datetime(2024, 1, 6, 10, 0, tzinfo=KST)

        result = utils.determine_trading_session(now)
        assert result["session"] == "CLOSED"

    def test_closed_on_holiday(self, trading_calendar):
        """Ensure holidays remain closed despite being within session hours."""
        trading_calendar({date(2024, 1, 2)})
        now = datetime(2024, 1, 1, 9, 30, tzinfo=KST)

        result = utils.determine_trading_session(now)
        assert result["session"] == "CLOSED"


class TestComputeNextOpen:
    """Verify compute_next_open_kst returns the next valid market opening."""

    def test_pre_market_moves_to_day_open(self, trading_calendar):
        """Ensure pre-market hours jump to the same day's opening bell."""
        trading_calendar({date(2024, 1, 2), date(2024, 1, 3)})
        now = datetime(2024, 1, 2, 8, 0, tzinfo=KST)

        expected = datetime(2024, 1, 2, 9, 0, tzinfo=KST)
        assert utils.compute_next_open_kst(now) == expected

    def test_during_day_session_moves_to_night_open(self, trading_calendar):
        """Ensure intra-day times point to the upcoming night session."""
        trading_calendar({date(2024, 1, 2), date(2024, 1, 3)})
        now = datetime(2024, 1, 2, 13, 0, tzinfo=KST)

        expected = datetime(2024, 1, 2, 18, 0, tzinfo=KST)
        assert utils.compute_next_open_kst(now) == expected

    def test_during_night_session_advances_to_next_day(self, trading_calendar):
        """Ensure active night trading advances to the next day session."""
        trading_calendar({date(2024, 1, 2), date(2024, 1, 3)})
        now = datetime(2024, 1, 2, 19, 30, tzinfo=KST)

        expected = datetime(2024, 1, 3, 9, 0, tzinfo=KST)
        assert utils.compute_next_open_kst(now) == expected

    def test_weekend_midday_advances_to_next_trading_day(self, trading_calendar):
        """Ensure weekends roll forward to the next trading morning."""
        trading_calendar({date(2024, 1, 5), date(2024, 1, 8)})
        now = datetime(2024, 1, 6, 10, 0, tzinfo=KST)

        expected = datetime(2024, 1, 8, 9, 0, tzinfo=KST)
        assert utils.compute_next_open_kst(now) == expected

    def test_holiday_moves_to_following_trading_day(self, trading_calendar):
        """Ensure holidays roll to the next available trading day."""
        trading_calendar({date(2024, 1, 2), date(2024, 1, 3)})
        now = datetime(2024, 1, 1, 11, 0, tzinfo=KST)

        expected = datetime(2024, 1, 2, 9, 0, tzinfo=KST)
        assert utils.compute_next_open_kst(now) == expected

    def test_early_morning_after_closed_day_targets_same_day(self, trading_calendar):
        """Ensure early mornings after a closed prior day point to the same day's open."""
        trading_calendar({date(2024, 1, 8), date(2024, 1, 9)})
        now = datetime(2024, 1, 8, 4, 0, tzinfo=KST)

        expected = datetime(2024, 1, 8, 9, 0, tzinfo=KST)
        assert utils.compute_next_open_kst(now) == expected

    def test_weekend_night_session_advances_to_next_weekday(self, trading_calendar):
        """Ensure weekend night trading advances to the following weekday morning."""
        trading_calendar({date(2024, 1, 5), date(2024, 1, 8)})
        now = datetime(2024, 1, 6, 2, 0, tzinfo=KST)

        expected = datetime(2024, 1, 8, 9, 0, tzinfo=KST)
        assert utils.compute_next_open_kst(now) == expected
