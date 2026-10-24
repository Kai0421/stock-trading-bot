# Author: Kai0421
"""The intraday strategy interface: what to buy during the session and when to get out.

Intraday mode (``TRADING_MODE=intraday``, docs/17-intraday.md) trades N-minute bars
inside one session and is always flat by ``flatten_at``: no position is held overnight.
Everything that is not the trading decision is shared with swing mode: the probability
gate (``bot.evidence``), sizing and cash checks (``bot.risk.approve_buys``), kill
switches, broker-side stops and the order path.

Hard rules the base class enforces for every intraday strategy (AGENTS.md):

- Long only: setups are buys, exits sell at most the shares held.
- Every entry has a stop strictly between 0 and the entry price (``stop_for``).
- Stops only move up (``manage``).
- No look-ahead: row ``i`` of ``prepare``'s output may only use bars ``<= i`` and daily
  bars of *earlier* sessions (``attach_daily`` shifts them by one session).
- Entries only between ``entry_start`` and ``entry_end``; at most one entry per symbol
  per session; every position is closed by ``flatten_at``.

Bars are indexed by the bar's *start* time, America/New_York, tz-naive, regular hours
only (09:30 to 16:00). A 5-minute bar stamped 10:00 closes at 10:05.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from bot.indicators import ema, rsi, sma
from bot.strategies.base import bucket_series, floor_cents, require_finite
from bot.strategies.common import ohlcv_frame

__all__ = [
    "IntradayPosition",
    "IntradayStrategy",
    "attach_daily",
    "bucket_series",
    "floor_cents",
    "minute_of_day",
    "session_features",
]

REQUIRED_COLUMNS = ("open", "high", "low", "close", "atr", "day", "minute")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


def minute_of_day(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass(frozen=True)
class IntradayPosition:
    symbol: str
    entry_price: float
    initial_stop: float
    stop: float
    shares: int
    bars_held: int  # completed bars since the entry bar opened

    def __post_init__(self) -> None:
        if not 0 < self.initial_stop < self.entry_price:
            raise ValueError("initial stop must be below the entry price")

    def r_multiple(self, price: float) -> float:
        return (price - self.entry_price) / (self.entry_price - self.initial_stop)


def session_features(bars: pd.DataFrame, atr_period: int = 14, ema_period: int = 20):
    """OHLCV plus the columns every intraday strategy uses.

    ``day`` (session date), ``minute`` (bar start, minutes after midnight), ``bar_no``
    (0 = the opening bar), ``day_open``, running ``day_high`` / ``day_low``, ``atr``
    (Wilder average of the bar range high - low, so overnight gaps don't inflate it),
    ``ema`` and ``rsi2`` of the closes.
    """
    f = ohlcv_frame(bars)
    idx = pd.DatetimeIndex(f.index)
    f["day"] = idx.normalize()
    f["minute"] = idx.hour * 60 + idx.minute
    g = f.groupby("day", sort=False)
    f["bar_no"] = g.cumcount()
    f["day_open"] = g["open"].transform("first")
    f["day_high"] = g["high"].cummax()
    f["day_low"] = g["low"].cummin()
    rng = f["high"] - f["low"]
    f["atr"] = rng.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()
    f["ema"] = ema(f["close"], ema_period) if len(f) >= ema_period else np.nan
    f["rsi2"] = rsi(f["close"], 2) if len(f) > 2 else np.nan
    return f


def attach_daily(
    f: pd.DataFrame,
    daily: pd.DataFrame | None,
    spy_daily: pd.DataFrame | None,
    sma_trend: int = 50,
    sma_regime: int = 200,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Add the *previous* session's daily context to every intraday bar.

    ``prev_close``, ``prev_sma`` (SMA ``sma_trend``), ``prev_sma_long`` (SMA
    ``sma_regime``), ``daily_atr`` (range average) and ``regime_ok`` (SPY's previous
    close above its SMA ``sma_regime``). Missing daily data leaves NaN / False, which
    fails closed (no setup).
    """
    days = pd.DatetimeIndex(f["day"])
    nan = pd.Series(np.nan, index=f.index)
    for col in ("prev_close", "prev_sma", "prev_sma_long", "daily_atr"):
        f[col] = nan
    f["regime_ok"] = False
    if daily is not None and len(daily):
        d = pd.DataFrame(index=daily.index)
        d["prev_close"] = daily["close"]
        d["prev_sma"] = daily["close"].rolling(sma_trend, min_periods=sma_trend).mean()
        d["prev_sma_long"] = daily["close"].rolling(sma_regime, min_periods=sma_regime).mean()
        rng = daily["high"] - daily["low"]
        d["daily_atr"] = rng.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()
        known = _before(d, days)
        for col in d.columns:
            f[col] = known[col].to_numpy()
    if spy_daily is not None and len(spy_daily):
        s = spy_daily["close"]
        ok = (s > sma(s, sma_regime)) if len(s) >= sma_regime else pd.Series(False, index=s.index)
        f["regime_ok"] = _before(ok.to_frame("ok"), days)["ok"].eq(1.0).to_numpy()
    return f


def _before(daily: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """For each session in ``days``, the last daily row dated strictly before it: what
    was known before that session opened."""
    index = pd.DatetimeIndex(daily.index).normalize()
    pos = index.searchsorted(days, side="left") - 1
    out = daily.iloc[np.clip(pos, 0, None)].astype("float64")
    out.index = days
    out[pos < 0] = np.nan
    return out


class IntradayStrategy(ABC):
    """A pluggable intraday algorithm. ``config`` is a frozen dataclass of parameters."""

    name: ClassVar[str]
    description: ClassVar[str] = ""
    bar_minutes: ClassVar[int] = 5
    # Symbols this strategy may trade (None = the whole universe).
    symbols: ClassVar[tuple[str, ...] | None] = None
    stability_params: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Any = None) -> None:
        self.config = self.default_config() if config is None else config

    @classmethod
    @abstractmethod
    def default_config(cls) -> Any:
        """Frozen dataclass with ``entry_start``, ``entry_end``, ``flatten_at`` (times)
        and ``limit_offset_pct`` plus the strategy's own parameters."""

    def with_config(self, config: Any) -> IntradayStrategy:
        return type(self)(config)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.config!r})"

    # ------------------------------------------------------------ to implement

    @abstractmethod
    def features(
        self, bars: pd.DataFrame, daily: pd.DataFrame | None, spy_daily: pd.DataFrame | None
    ) -> pd.DataFrame:
        """Intraday bars of one symbol (many sessions) -> features plus ``setup``,
        ``signature`` and ``score`` columns. Use ``session_features`` and
        ``attach_daily``; never read a later bar or the same session's daily bar."""

    @abstractmethod
    def initial_stop(self, entry: float, bar: Mapping[str, float]) -> float:
        """Protective stop for an entry at ``entry`` after signal bar ``bar``; 1R."""

    @abstractmethod
    def on_bar(self, pos: IntradayPosition, bar: Mapping[str, float]) -> tuple[str | None, float]:
        """After each completed bar: an exit reason (sell everything at the next bar's
        open) or None, and the new stop."""

    # ------------------------------------------------------------ provided

    def limit_price(self, last_price: float) -> float:
        if not last_price > 0:
            raise ValueError(f"last price must be positive (got {last_price})")
        return round(last_price * (1 + self.config.limit_offset_pct), 2)

    def stop_for(self, entry: float, bar: Mapping[str, float]) -> float:
        stop = self.initial_stop(entry, bar)
        if stop is None or not math.isfinite(stop) or not 0 < stop < entry:
            raise ValueError(f"stop {stop} is not between 0 and entry {entry}")
        return stop

    def manage(self, pos: IntradayPosition, bar: Mapping[str, float]) -> tuple[str | None, float]:
        """``on_bar`` with the hard rule applied: the stop never moves down."""
        reason, stop = self.on_bar(pos, bar)
        stop = pos.stop if stop is None or not math.isfinite(stop) else max(pos.stop, stop)
        return reason, stop

    def prepare(
        self, bars: pd.DataFrame, daily: pd.DataFrame | None, spy_daily: pd.DataFrame | None
    ) -> pd.DataFrame:
        """Validated features: setups only inside the entry window, signatures
        namespaced ``<name>|...``."""
        cfg = self.config
        f = self.features(bars, daily, spy_daily)
        missing = [c for c in (*REQUIRED_COLUMNS, "setup", "signature", "score") if c not in f]
        if missing:
            raise ValueError(f"{self.name} features are missing {missing}")
        if not f.index.equals(bars.index):
            raise ValueError(f"{self.name} features are not aligned to the bars")
        bar_end = f["minute"] + self.bar_minutes
        window = (bar_end > minute_of_day(cfg.entry_start)) & (
            bar_end <= minute_of_day(cfg.entry_end)
        )
        setup = f["setup"].fillna(False).astype(bool) & window
        f["setup"] = setup
        sig = f["signature"].astype(object).where(f["signature"].notna(), "na").astype(str)
        f["signature"] = (self.name + "|" + sig).where(setup, None)
        return f


def check_times(entry_start: time, entry_end: time, flatten_at: time) -> None:
    from bot.config import ConfigError

    if not SESSION_OPEN < entry_start < entry_end < flatten_at < SESSION_CLOSE:
        raise ConfigError(
            "need 09:30 < entry_start < entry_end < flatten_at < 16:00 "
            f"(got {entry_start}, {entry_end}, {flatten_at})"
        )


def atr_floor_stop(entry: float, level: float, atr_value: float, max_atr: float) -> float:
    """Stop at ``level`` but never more than ``max_atr`` ATRs below the entry."""
    require_finite(level=level, atr=atr_value)
    if not atr_value > 0:
        raise ValueError(f"ATR must be positive (got {atr_value})")
    return floor_cents(max(level, entry - max_atr * atr_value))
