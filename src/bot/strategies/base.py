# Author: Kai0421
"""The strategy interface: everything that decides *what* to trade and *when to exit*.

A strategy turns daily bars into entry setups and manages open positions on the daily
close. Everything else (the probability gate, sizing, cash checks, kill switches, order
placement and fills) stays in ``bot.risk``, ``bot.backtest`` and ``bot.engine`` and is the
same for every strategy, so strategies can be swapped and compared on equal terms.

Hard rules the base class enforces for every strategy (AGENTS.md):

- Long only: a strategy can only produce buy setups and sell exits of shares it holds.
- Every entry has a stop strictly between 0 and the entry price (``stop_for``).
- Stops only move up (``manage``), and exits never sell more shares than are held.
- No look-ahead: row ``i`` of a strategy's entry table may only use bars ``<= i``.

To add a strategy, subclass ``Strategy``, implement the four abstract methods and
register it in ``bot.strategies.REGISTRY`` (docs/12-strategies.md).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np
import pandas as pd

OHLCV = ("open", "high", "low", "close", "volume")
# Columns the backtester and engine read from every strategy's features.
REQUIRED_FEATURES = ("open", "high", "low", "close", "atr")
# Columns the backtester and engine read from every strategy's entry table.
REQUIRED_TABLE = ("setup", "signature", "score")


@dataclass(frozen=True)
class PositionState:
    symbol: str
    entry_price: float
    initial_stop: float
    stop: float
    shares: int
    days_held: int  # trading days since entry
    partial_taken: bool

    def __post_init__(self) -> None:
        if not 0 < self.initial_stop < self.entry_price:
            raise ValueError("initial stop must be below the entry price")

    def r_multiple(self, price: float) -> float:
        return (price - self.entry_price) / (self.entry_price - self.initial_stop)


@dataclass(frozen=True)
class ExitAction:
    kind: str  # e.g. "trend_break" | "time_stop" | "partial_profit" | "profit_target"
    shares: int


def floor_cents(price: float) -> float:
    # The epsilon stops values like 98.8 (98.79999...) from flooring a cent too low.
    return math.floor(price * 100 + 1e-6) / 100


def require_finite(**values: float) -> None:
    bad = [k for k, v in values.items() if v is None or not math.isfinite(v)]
    if bad:
        raise ValueError(f"non-finite inputs: {bad}")


def bucket_series(values: pd.Series, edges: tuple[float, float], labels: tuple[str, ...]):
    """``labels[0]`` below ``edges[0]``, ``labels[1]`` below ``edges[1]``, else
    ``labels[2]``, element-wise; NaN becomes None. Used to build setup signatures."""
    v = values.to_numpy(dtype="float64")
    out = np.where(v < edges[0], labels[0], np.where(v < edges[1], labels[1], labels[2]))
    out = out.astype(object)
    out[np.isnan(v)] = None
    return pd.Series(out, index=values.index)


class Strategy(ABC):
    """A pluggable trading algorithm. Instances are immutable: ``config`` is a frozen
    dataclass of parameters, and ``with_config`` returns a new instance."""

    name: ClassVar[str]
    description: ClassVar[str] = ""
    # Parameters the ±20% stability check perturbs (fields of ``config``).
    stability_params: ClassVar[tuple[str, ...]] = ()
    # Signatures are namespaced with "<name>|" so evidence from different strategies can
    # never be mixed up. Trend Pullback keeps "" for its pre-existing evidence.
    signature_prefix: ClassVar[str | None] = None

    def __init__(self, config: Any = None) -> None:
        self.config = self.default_config() if config is None else config

    @classmethod
    @abstractmethod
    def default_config(cls) -> Any:
        """A frozen dataclass with this strategy's default parameters."""

    def with_config(self, config: Any) -> Strategy:
        return type(self)(config)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.config!r})"

    # ------------------------------------------------------------ to implement

    @abstractmethod
    def features(self, bars: pd.DataFrame) -> pd.DataFrame:
        """OHLCV plus indicator columns, including at least ``REQUIRED_FEATURES``.
        The input is not modified."""

    @abstractmethod
    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        """Per symbol, one row per bar with ``setup`` (bool), ``signature`` (str when
        ``setup``, else None) and ``score`` (higher ranks first). Row ``i`` may only use
        rows ``<= i``. ``features[benchmark]`` is there for a market-regime filter."""

    @abstractmethod
    def initial_stop(self, entry: float, atr: float) -> float:
        """Protective stop for a new position entered at ``entry``; defines 1R."""

    @abstractmethod
    def on_close(
        self, pos: PositionState, bar: Mapping[str, float]
    ) -> tuple[list[ExitAction], float]:
        """After the daily close: exits to execute at the next open, and the new stop.
        ``bar`` is the last row of ``features`` for the symbol."""

    # ------------------------------------------------------------ provided

    def limit_price(self, last_price: float) -> float:
        """Entry limit = last price + ``limit_offset_pct``, rounded to the cent."""
        if not last_price > 0:
            raise ValueError(f"last price must be positive (got {last_price})")
        return round(last_price * (1 + self.config.limit_offset_pct), 2)

    def stop_for(self, entry: float, atr: float) -> float:
        """``initial_stop`` checked against the hard rule: 0 < stop < entry."""
        stop = self.initial_stop(entry, atr)
        if not 0 < stop < entry:
            raise ValueError(f"stop {stop} is not between 0 and entry {entry}")
        return stop

    def manage(
        self, pos: PositionState, bar: Mapping[str, float]
    ) -> tuple[list[ExitAction], float]:
        """``on_close`` with the hard rules applied: the stop never moves down, and at
        most one exit, of at most the shares held, is returned."""
        actions, stop = self.on_close(pos, bar)
        stop = pos.stop if stop is None or not math.isfinite(stop) else max(pos.stop, stop)
        out = [a for a in actions if a.shares > 0][:1]
        if out and out[0].shares > pos.shares:
            out = [ExitAction(out[0].kind, pos.shares)]
        return out, stop

    def prepare(
        self, bars: Mapping[str, pd.DataFrame], benchmark: str
    ) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
        """Features and validated entry tables for every symbol."""
        if benchmark not in bars:
            raise ValueError(f"benchmark {benchmark} bars are required for the market regime")
        features = {s: self.features(df) for s, df in bars.items()}
        for s, f in features.items():
            missing = [c for c in REQUIRED_FEATURES if c not in f.columns]
            if missing:
                raise ValueError(f"{self.name} features for {s} are missing {missing}")
        tables = self.entry_tables(features, benchmark)
        prefix = self.name + "|" if self.signature_prefix is None else self.signature_prefix
        for s, t in tables.items():
            missing = [c for c in REQUIRED_TABLE if c not in t.columns]
            if missing:
                raise ValueError(f"{self.name} entry table for {s} is missing {missing}")
            if not t.index.equals(features[s].index):
                raise ValueError(f"{self.name} entry table for {s} is not aligned to its bars")
            setup = t["setup"].fillna(False).astype(bool)
            t["setup"] = setup
            if prefix:
                t["signature"] = (prefix + t["signature"].astype(str)).where(setup, None)
        return features, tables
