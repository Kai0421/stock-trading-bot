# Author: Kai0421
"""Tests for bot.calendar.

Fixtures come from docs/02-market-hours-holidays.md (NYSE, fetched 2026-10-04).
"""

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.calendar import (
    ET,
    AlpacaCalendarSource,
    CalendarUnavailableError,
    FallbackNYSECalendar,
    MarketCalendar,
    Session,
)

UTC = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")


def et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


class FakeSource:
    """A primary source that returns fixed sessions, or raises if broken."""

    def __init__(self, sessions=None, error=None):
        self._sessions = sessions or []
        self._error = error
        self.calls = 0

    def sessions(self, start, end):
        self.calls += 1
        if self._error:
            raise self._error
        return [s for s in self._sessions if start <= s.day <= end]


class BrokenSource(FakeSource):
    def __init__(self):
        super().__init__(error=ConnectionError("api down"))


# ---------------------------------------------------------------- Session


def test_session_regular_and_early_close():
    regular = Session(date(2026, 10, 5), et(2026, 10, 5, 9, 30), et(2026, 10, 5, 16, 0))
    early = Session(date(2026, 11, 27), et(2026, 11, 27, 9, 30), et(2026, 11, 27, 13, 0))
    assert not regular.is_early_close
    assert early.is_early_close


def test_session_requires_tz_aware_times():
    with pytest.raises(ValueError):
        Session(date(2026, 10, 5), datetime(2026, 10, 5, 9, 30), et(2026, 10, 5, 16, 0))


# ---------------------------------------------------------------- fallback table

HOLIDAYS_2026_2027 = [
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),
    date(2027, 7, 5),
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),
]


@pytest.mark.parametrize("day", HOLIDAYS_2026_2027)
def test_fallback_holidays_are_closed(day):
    assert FallbackNYSECalendar().sessions(day, day) == []


@pytest.mark.parametrize("day", [date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)])
def test_fallback_early_closes_at_1pm(day):
    [s] = FallbackNYSECalendar().sessions(day, day)
    assert s.close == datetime.combine(day, time(13, 0), tzinfo=ET)
    assert s.is_early_close


def test_fallback_regular_day_hours_in_new_york_time():
    [s] = FallbackNYSECalendar().sessions(date(2026, 10, 5), date(2026, 10, 5))
    assert s.open == et(2026, 10, 5, 9, 30)
    assert s.close == et(2026, 10, 5, 16, 0)
    assert s.open.tzinfo == ET


def test_fallback_weekends_closed_and_bank_holidays_open():
    cal = FallbackNYSECalendar()
    # Sat/Sun 2026-10-10/11 closed; Columbus Day Mon 2026-10-12 and
    # Veterans Day Wed 2026-11-11 are normal NYSE trading days.
    days = [s.day for s in cal.sessions(date(2026, 10, 9), date(2026, 10, 12))]
    assert days == [date(2026, 10, 9), date(2026, 10, 12)]
    assert [s.day for s in cal.sessions(date(2026, 11, 11), date(2026, 11, 11))] == [
        date(2026, 11, 11)
    ]


def test_fallback_trading_day_count_for_a_year_is_plausible():
    # 2026: 261 weekdays - 10 weekday holidays = 251 sessions
    assert len(FallbackNYSECalendar().sessions(date(2026, 1, 1), date(2026, 12, 31))) == 251


def test_fallback_refuses_to_guess_outside_its_table():
    cal = FallbackNYSECalendar()
    with pytest.raises(CalendarUnavailableError):
        cal.sessions(date(2025, 12, 31), date(2026, 1, 2))
    with pytest.raises(CalendarUnavailableError):
        cal.sessions(date(2028, 1, 3), date(2028, 1, 3))


# ---------------------------------------------------------------- primary vs fallback


def test_uses_primary_source_when_available():
    special = Session(date(2026, 10, 5), et(2026, 10, 5, 9, 30), et(2026, 10, 5, 13, 0))
    primary = FakeSource([special])
    cal = MarketCalendar(primary=primary)
    assert cal.session_for(date(2026, 10, 5)) == special
    assert cal.last_source == "primary"
    assert primary.calls == 1


def test_falls_back_when_primary_fails():
    cal = MarketCalendar(primary=BrokenSource())
    assert cal.is_trading_day(date(2026, 10, 5))
    assert not cal.is_trading_day(date(2026, 11, 26))
    assert cal.last_source == "fallback"


def test_no_primary_uses_fallback():
    cal = MarketCalendar(primary=None)
    assert cal.is_early_close(date(2026, 11, 27))
    assert cal.last_source == "fallback"


def test_raises_when_no_source_can_answer():
    cal = MarketCalendar(primary=BrokenSource())
    with pytest.raises(CalendarUnavailableError):
        cal.sessions(date(2030, 1, 2), date(2030, 1, 4))


# ---------------------------------------------------------------- is_open


def test_is_open_requires_tz_aware_now():
    with pytest.raises(ValueError):
        MarketCalendar().is_open(datetime(2026, 10, 5, 10, 0))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 5, 13, 29, tzinfo=UTC), False),  # 09:29 EDT
        (datetime(2026, 10, 5, 13, 30, tzinfo=UTC), True),  # 09:30 EDT
        (datetime(2026, 10, 5, 19, 59, tzinfo=UTC), True),  # 15:59 EDT
        (datetime(2026, 10, 5, 20, 0, tzinfo=UTC), False),  # 16:00 EDT, closed
        (et(2026, 10, 10, 12, 0), False),  # Saturday
        (et(2026, 11, 26, 12, 0), False),  # Thanksgiving
        (et(2026, 11, 27, 12, 59), True),  # early close day, before 13:00
        (et(2026, 11, 27, 13, 0), False),  # early close day, after 13:00
    ],
)
def test_is_open(now, expected):
    assert MarketCalendar().is_open(now) is expected


def test_is_open_handles_london_time_across_mismatched_dst():
    # Europe leaves summer time on 2026-10-25, the US on 2026-11-01.
    # On 2026-10-27 London is UTC+0 and New York UTC-4, so the open is 13:30 London.
    cal = MarketCalendar()
    assert not cal.is_open(datetime(2026, 10, 27, 13, 29, tzinfo=LONDON))
    assert cal.is_open(datetime(2026, 10, 27, 13, 30, tzinfo=LONDON))
    # A week earlier both are on summer time: open is 14:30 London.
    assert not cal.is_open(datetime(2026, 10, 20, 14, 29, tzinfo=LONDON))
    assert cal.is_open(datetime(2026, 10, 20, 14, 30, tzinfo=LONDON))


def test_is_open_fails_closed_when_calendar_unavailable():
    cal = MarketCalendar(primary=BrokenSource())
    assert cal.is_open(et(2030, 1, 2, 11, 0)) is False


# ---------------------------------------------------------------- trading windows


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (et(2026, 10, 5, 9, 44), False),  # first 15 minutes
        (et(2026, 10, 5, 9, 45), True),
        (et(2026, 10, 5, 15, 49), True),
        (et(2026, 10, 5, 15, 50), False),  # last 10 minutes
        (et(2026, 11, 27, 12, 49), True),  # early close: window ends 12:50
        (et(2026, 11, 27, 12, 50), False),
        (et(2026, 10, 10, 11, 0), False),  # weekend
    ],
)
def test_in_order_window(now, expected):
    assert MarketCalendar().in_order_window(now) is expected


def test_entries_allowed_only_in_window_and_never_on_early_close_days():
    cal = MarketCalendar()
    assert cal.entries_allowed(et(2026, 10, 5, 9, 45))
    assert not cal.entries_allowed(et(2026, 10, 5, 9, 40))
    assert not cal.entries_allowed(et(2026, 11, 27, 10, 0))  # early close day
    assert not cal.entries_allowed(et(2026, 12, 24, 10, 0))  # early close day


def test_windows_fail_closed_when_calendar_unavailable():
    cal = MarketCalendar(primary=BrokenSource())
    assert cal.in_order_window(et(2030, 1, 2, 11, 0)) is False
    assert cal.entries_allowed(et(2030, 1, 2, 11, 0)) is False


# ---------------------------------------------------------------- navigation


def test_next_and_previous_session_skip_holidays_and_weekends():
    cal = MarketCalendar()
    assert cal.next_session(date(2026, 11, 25)).day == date(2026, 11, 27)
    assert cal.next_session(date(2026, 7, 2)).day == date(2026, 7, 6)
    assert cal.previous_session(date(2026, 11, 30)).day == date(2026, 11, 27)


def test_previous_session_before_table_raises():
    with pytest.raises(CalendarUnavailableError):
        MarketCalendar().previous_session(date(2026, 1, 2))


def test_add_trading_days():
    cal = MarketCalendar()
    assert cal.add_trading_days(date(2026, 11, 25), 1) == date(2026, 11, 27)
    assert cal.add_trading_days(date(2026, 11, 25), 0) == date(2026, 11, 25)
    # 5 trading days after Wed 2026-12-23: 24, 28, 29, 30, 31
    assert cal.add_trading_days(date(2026, 12, 23), 5) == date(2026, 12, 31)
    with pytest.raises(ValueError):
        cal.add_trading_days(date(2026, 11, 25), -1)


def test_trading_days_between_inclusive():
    days = MarketCalendar().trading_days(date(2026, 11, 23), date(2026, 11, 29))
    assert days == [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27)]


# ---------------------------------------------------------------- Alpaca adapter


class FakeTradingClient:
    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    def get_calendar(self, filters=None):
        self.requests.append(filters)
        return self.rows


def test_alpaca_source_converts_naive_et_rows_to_sessions():
    rows = [
        SimpleNamespace(
            date=date(2026, 11, 27),
            open=datetime(2026, 11, 27, 9, 30),
            close=datetime(2026, 11, 27, 13, 0),
        )
    ]
    client = FakeTradingClient(rows)
    src = AlpacaCalendarSource(client, request_factory=lambda s, e: ("req", s, e))
    [s] = src.sessions(date(2026, 11, 27), date(2026, 11, 27))
    assert client.requests == [("req", date(2026, 11, 27), date(2026, 11, 27))]
    assert s.day == date(2026, 11, 27)
    assert s.open == et(2026, 11, 27, 9, 30)
    assert s.close == et(2026, 11, 27, 13, 0)
    assert s.is_early_close


def test_alpaca_source_accepts_aware_rows_and_filters_range():
    rows = [
        SimpleNamespace(
            date=date(2026, 10, 5),
            open=datetime(2026, 10, 5, 13, 30, tzinfo=UTC),
            close=datetime(2026, 10, 5, 20, 0, tzinfo=UTC),
        ),
        SimpleNamespace(  # outside the requested range, must be dropped
            date=date(2026, 10, 6),
            open=datetime(2026, 10, 6, 9, 30),
            close=datetime(2026, 10, 6, 16, 0),
        ),
    ]
    src = AlpacaCalendarSource(FakeTradingClient(rows), request_factory=lambda s, e: None)
    [s] = src.sessions(date(2026, 10, 5), date(2026, 10, 5))
    assert s.open == et(2026, 10, 5, 9, 30)
    assert s.close - s.open == timedelta(hours=6, minutes=30)


def test_alpaca_source_error_triggers_fallback():
    class Exploding:
        def get_calendar(self, filters=None):
            raise TimeoutError("slow")

    cal = MarketCalendar(
        primary=AlpacaCalendarSource(Exploding(), request_factory=lambda s, e: None)
    )
    assert cal.is_trading_day(date(2026, 10, 5))
    assert cal.last_source == "fallback"
