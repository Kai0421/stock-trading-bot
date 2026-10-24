# Author: Kai0421
"""Data layer: every read the engine makes, returned as plain snapshots (docs/19-layers.md).

The data layer reads the broker (account, positions, open orders, order lookups, asset
checks), market data (daily and intraday bars, latest prices), alternative data, the
backtest evidence file and the journal's book (positions, flags, live outcomes). It
never writes anything and never decides anything: ``bot.decide`` turns its snapshots
into decisions and ``bot.execute`` carries them out.

Freshness matters for the hard rules, so reads are not cached: ``account`` is fetched
again right before every order (docs/01), and ``book`` is re-read after every
reconcile.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from bot.broker import AccountSnapshot, BrokerOrder, BrokerPosition
from bot.evidence import Evidence, EvidenceGate, evidence_file, load_evidence
from bot.journal import PositionRow
from bot.sentiment import DEFAULT_DIR, Snapshot, latest_fresh, read_snapshots

ET = ZoneInfo("America/New_York")

BarLoader = Callable[[Sequence[str], date, date | datetime], dict[str, pd.DataFrame]]
IntradayLoader = Callable[[Sequence[str], datetime, datetime], dict[str, pd.DataFrame]]


@dataclass(frozen=True)
class Book:
    """Broker positions and open orders plus the journal's management rows, read together."""

    positions: dict[str, BrokerPosition]
    open_orders: list[BrokerOrder]
    rows: dict[str, PositionRow]


@dataclass(frozen=True)
class Fresh:
    """A fresh account fetch for one order: account, open orders and positions."""

    snap: AccountSnapshot
    open_orders: list[BrokerOrder]
    positions: dict[str, BrokerPosition]


@dataclass(frozen=True)
class Flags:
    paused: bool
    halt_today: bool
    halt_day: str | None


class Gatherer:
    """Read-only access to the broker, market data, alt-data, evidence and journal."""

    def __init__(
        self,
        broker: Any,
        journal: Any,
        load_bars: BarLoader,
        latest_prices: Callable[[Sequence[str]], dict[str, float]],
        load_intraday: IntradayLoader | None,
        altdata: Callable[[str, date], dict[str, bool]] | None,
        evidence_path: Path,
        sentiment_dir: Path = DEFAULT_DIR,
    ) -> None:
        self.broker = broker
        self.journal = journal
        self._load_bars = load_bars
        self._latest_prices = latest_prices
        self._load_intraday = load_intraday
        self._altdata = altdata
        self.evidence_path = evidence_path
        self.sentiment_dir = sentiment_dir

    # ----------------------------------------------------------- broker / journal

    def book(self) -> Book:
        return Book(self.broker.positions(), self.broker.open_orders(), self.journal.positions())

    def fresh(self) -> Fresh:
        """Account, open orders and positions, fetched now (raises ``BrokerError``)."""
        snap = self.broker.account()
        return Fresh(snap, self.broker.open_orders(), self.broker.positions())

    def order_exists(self, cid: str) -> bool:
        """Already recorded or already at the broker: never send it twice."""
        return self.journal.has_order(cid) or self.broker.order_by_client_id(cid) is not None

    def asset_problem(self, symbol: str) -> str | None:
        return self.broker.check_asset(symbol)

    def flags(self) -> Flags:
        j = self.journal
        return Flags(j.flag("paused"), j.flag("halt_today"), j.get_kv("halt_day"))

    # ----------------------------------------------------------- market data

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date | datetime
    ) -> dict[str, pd.DataFrame]:
        return self._load_bars(list(symbols), start, end)

    def price(self, symbol: str) -> float | None:
        return self._latest_prices([symbol]).get(symbol)

    def today_bars(
        self, symbols: Sequence[str], now: datetime, minutes: int
    ) -> dict[str, pd.DataFrame]:
        """Today's completed intraday bars (a bar stamped t closes at t + minutes)."""
        if self._load_intraday is None:
            raise RuntimeError("intraday mode needs an intraday bar loader")
        start = datetime.combine(now.date(), time(9, 30), tzinfo=ET)
        out = {}
        cutoff = pd.Timestamp(now.replace(tzinfo=None)) - pd.Timedelta(minutes=minutes)
        for s, df in self._load_intraday(list(symbols), start, now).items():
            done = df[df.index <= cutoff]
            if not done.empty:
                out[s] = done
        return out

    def altdata(
        self, symbols: Sequence[str], day: date, features: frozenset[str]
    ) -> dict[str, dict[str, bool]] | None:
        """Alt-data features per symbol (None when no provider or no features are on)."""
        if self._altdata is None or not features:
            return None
        return {s: self._altdata(s, day) for s in symbols}

    def sentiment(
        self, symbols: Sequence[str], now: datetime, max_age_minutes: int
    ) -> dict[str, Snapshot]:
        """Fresh Stocktwits snapshots for ``symbols`` (empty when the file is missing or old)."""
        snaps = read_snapshots(self.sentiment_dir / "snapshots.jsonl")
        return latest_fresh(snaps, symbols, now, max_age_minutes)

    # ----------------------------------------------------------- evidence

    def evidence(self, strategy: str, universe: str = "sp100") -> tuple[Evidence | None, Path]:
        path = evidence_file(self.evidence_path, strategy, universe)
        return load_evidence(path), path

    def gate(self, evidence: Evidence, paper_trial: bool) -> EvidenceGate:
        """The live gate: backtest evidence plus the journal's live outcomes."""
        return EvidenceGate(evidence, self.journal.outcomes_by_signature(), paper_trial)
