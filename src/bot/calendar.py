# Author: Kai0421
"""NYSE market calendar: sessions, early closes and trading windows.

Primary source is the Alpaca calendar API. A local table of NYSE holidays and early
closes (docs/02-market-hours-holidays.md) is the fallback when the API is unreachable.

Safety rules:
- All public time checks need timezone-aware datetimes; naive ones are rejected so
  the host's local time zone (e.g. Europe/London) can never be mistaken for New York time.
- If no source can answer, the time checks fail closed: market treated as closed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

# No orders in the first 15 or the last 10 minutes of a session (docs/03-strategy.md).
OPEN_BUFFER = timedelta(minutes=15)
CLOSE_BUFFER = timedelta(minutes=10)


class CalendarUnavailableError(RuntimeError):
    """No calendar source could answer for the requested dates."""


@dataclass(frozen=True)
class Session:
    """One trading session. ``open`` and ``close`` are timezone-aware."""

    day: date
    open: datetime
    close: datetime

    def __post_init__(self) -> None:
        for name in ("open", "close"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"Session.{name} must be timezone-aware")
        if self.close <= self.open:
            raise ValueError("Session.close must be after Session.open")

    @property
    def is_early_close(self) -> bool:
        return self.close.astimezone(ET).time() < REGULAR_CLOSE

    def contains(self, now: datetime) -> bool:
        return self.open <= now < self.close


class CalendarSource(Protocol):
    def sessions(self, start: date, end: date) -> list[Session]:
        """Sessions with ``start <= day <= end``, in date order."""
        ...


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware (e.g. tzinfo=ET)")


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------- fallback

# Source: nyse.com/markets/hours-calendars, fetched 2026-10-04.
NYSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # Martin Luther King Jr. Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day (observed)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),  # Juneteenth (observed)
        date(2027, 7, 5),  # Independence Day (observed)
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),  # Christmas (observed)
    }
)

NYSE_EARLY_CLOSES: dict[date, time] = {
    date(2026, 11, 27): time(13, 0),  # day after Thanksgiving
    date(2026, 12, 24): time(13, 0),  # Christmas Eve
    date(2027, 11, 26): time(13, 0),  # day after Thanksgiving
}

# The table is only trusted for the years it fully covers.
FALLBACK_FIRST_DAY = date(2026, 1, 1)
FALLBACK_LAST_DAY = date(2027, 12, 31)


class FallbackNYSECalendar:
    """Static NYSE calendar for 2026-2027. Refuses to guess outside that range."""

    def sessions(self, start: date, end: date) -> list[Session]:
        if start < FALLBACK_FIRST_DAY or end > FALLBACK_LAST_DAY:
            raise CalendarUnavailableError(
                f"fallback calendar covers {FALLBACK_FIRST_DAY}..{FALLBACK_LAST_DAY}, "
                f"asked for {start}..{end}"
            )
        out = []
        for d in _days(start, end):
            if d.weekday() >= 5 or d in NYSE_HOLIDAYS:
                continue
            close = NYSE_EARLY_CLOSES.get(d, REGULAR_CLOSE)
            out.append(
                Session(
                    d,
                    datetime.combine(d, REGULAR_OPEN, tzinfo=ET),
                    datetime.combine(d, close, tzinfo=ET),
                )
            )
        return out


# --------------------------------------------------------------------- Alpaca


def _alpaca_calendar_request(start: date, end: date) -> Any:
    from alpaca.trading.requests import GetCalendarRequest  # lazy: optional at import

    return GetCalendarRequest(start=start, end=end)


def _as_et(value: datetime) -> datetime:
    # Alpaca returns session times as naive New York wall-clock times.
    return value.replace(tzinfo=ET) if value.tzinfo is None else value.astimezone(ET)


class AlpacaCalendarSource:
    """Adapter over ``alpaca.trading.client.TradingClient.get_calendar``."""

    def __init__(
        self,
        client: Any,
        request_factory: Callable[[date, date], Any] = _alpaca_calendar_request,
    ) -> None:
        self._client = client
        self._request_factory = request_factory

    def sessions(self, start: date, end: date) -> list[Session]:
        rows = self._client.get_calendar(filters=self._request_factory(start, end))
        out = [
            Session(row.date, _as_et(row.open), _as_et(row.close))
            for row in rows
            if start <= row.date <= end
        ]
        return sorted(out, key=lambda s: s.day)


# --------------------------------------------------------------------- facade


class MarketCalendar:
    """Calendar used by the engine: primary source with a static fallback."""

    def __init__(
        self,
        primary: CalendarSource | None = None,
        fallback: CalendarSource | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback if fallback is not None else FallbackNYSECalendar()
        self.last_source: str | None = None

    # -- data

    def sessions(self, start: date, end: date) -> list[Session]:
        if self._primary is not None:
            try:
                result = self._primary.sessions(start, end)
                self.last_source = "primary"
                return result
            except Exception as exc:  # any API failure -> use the local table
                log.warning("primary calendar failed (%s); using fallback table", exc)
        try:
            result = self._fallback.sessions(start, end)
        except CalendarUnavailableError:
            self.last_source = None
            raise
        except Exception as exc:
            self.last_source = None
            raise CalendarUnavailableError(str(exc)) from exc
        self.last_source = "fallback"
        return result

    def session_for(self, day: date) -> Session | None:
        found = self.sessions(day, day)
        return found[0] if found else None

    def is_trading_day(self, day: date) -> bool:
        return self.session_for(day) is not None

    def is_early_close(self, day: date) -> bool:
        s = self.session_for(day)
        return s is not None and s.is_early_close

    def trading_days(self, start: date, end: date) -> list[date]:
        return [s.day for s in self.sessions(start, end)]

    def next_session(self, after: date) -> Session:
        """First session strictly after ``after``."""
        found = self.sessions(after + timedelta(days=1), after + timedelta(days=10))
        if not found:
            raise CalendarUnavailableError(f"no session within 10 days after {after}")
        return found[0]

    def previous_session(self, before: date) -> Session:
        """Last session strictly before ``before``."""
        found = self.sessions(before - timedelta(days=10), before - timedelta(days=1))
        if not found:
            raise CalendarUnavailableError(f"no session within 10 days before {before}")
        return found[-1]

    def add_trading_days(self, day: date, n: int) -> date:
        """The trading day ``n`` sessions after ``day`` (``n=0`` returns ``day``)."""
        if n < 0:
            raise ValueError("n must be >= 0")
        current = day
        for _ in range(n):
            current = self.next_session(current).day
        return current

    # -- time checks (fail closed)

    def _session_at(self, now: datetime) -> Session | None:
        _require_aware(now)
        try:
            return self.session_for(now.astimezone(ET).date())
        except CalendarUnavailableError:
            log.error("calendar unavailable for %s; treating market as closed", now)
            return None

    def is_open(self, now: datetime) -> bool:
        s = self._session_at(now)
        return s is not None and s.contains(now)

    def in_order_window(self, now: datetime) -> bool:
        """True when orders may be sent: not in the first 15 / last 10 minutes."""
        s = self._session_at(now)
        if s is None:
            return False
        return s.open + OPEN_BUFFER <= now < s.close - CLOSE_BUFFER

    def entries_allowed(self, now: datetime) -> bool:
        """New entries need the order window and are never placed on early-close days."""
        s = self._session_at(now)
        if s is None or s.is_early_close:
            return False
        return s.open + OPEN_BUFFER <= now < s.close - CLOSE_BUFFER
