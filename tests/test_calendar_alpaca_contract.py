# Author: Kai0421
"""Contract test: AlpacaCalendarSource against the real alpaca-py models."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from alpaca.trading.models import Calendar
from alpaca.trading.requests import GetCalendarRequest

from bot.calendar import AlpacaCalendarSource

ET = ZoneInfo("America/New_York")


class _Client:
    def __init__(self) -> None:
        self.filters: object = None

    def get_calendar(self, filters: object = None) -> list[Calendar]:
        self.filters = filters
        return [Calendar(date="2026-10-05", open="09:30", close="16:00")]


def test_real_request_and_rows() -> None:
    client = _Client()
    sessions = AlpacaCalendarSource(client).sessions(date(2026, 10, 5), date(2026, 10, 9))
    assert isinstance(client.filters, GetCalendarRequest)
    assert client.filters.start == date(2026, 10, 5)
    assert client.filters.end == date(2026, 10, 9)
    (s,) = sessions
    assert s.day == date(2026, 10, 5)
    assert s.open == datetime(2026, 10, 5, 9, 30, tzinfo=ET)
    assert s.close == datetime(2026, 10, 5, 16, 0, tzinfo=ET)
