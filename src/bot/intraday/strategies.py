# Author: Kai0421
"""Intraday strategy candidates (docs/17-intraday.md). Long only, flat by the close.

All four are slow, price-based setups on 5-minute bars: at most one entry per symbol per
session, no volume signals (Alpaca's free real-time feed is IEX only, so live volume is
a small slice of the market) and nothing that needs sub-minute fills. Parameters are
hypotheses until backtested (memory/decisions.md).

- ``orb_breakout``: first close above the 30-minute opening range in a daily uptrend.
  Stop at the range low (capped at 3 ATR), breakeven at +1R, then a 2 ATR trail.
- ``gap_fill``: gap down 0.5-3% in a daily uptrend, then a close back above the open.
  Sell when yesterday's close is reached; stop under the day's low.
- ``intraday_reversion``: RSI(2) < 5 and 1.5 ATR under the 20-bar EMA in a daily
  uptrend. Sell on a close above the EMA, RSI(2) > 70 or after 12 bars.
- ``late_momentum``: SPY/QQQ only; a first half hour up > 0.25% predicts the last half
  hour (Gao et al. 2018). Buy at 15:30, sell at 15:55.

Every strategy except ``late_momentum`` is flat by 15:50.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from typing import ClassVar

import numpy as np
import pandas as pd

from bot.config import ConfigError
from bot.intraday.base import (
    IntradayPosition,
    IntradayStrategy,
    atr_floor_stop,
    attach_daily,
    bucket_series,
    check_times,
    floor_cents,
    minute_of_day,
    session_features,
)
from bot.strategies.base import require_finite

_WIDTH = ("narrow", "mid", "wide")
_GAP = ("down", "flat", "up")
_SIZE = ("small", "mid", "large")
_TREND = ("near", "mid", "far")
_DEPTH = ("mild", "deep", "extreme")
_TOD = ("morning", "midday", "afternoon")


def _check_edges(cfg: object, *names: str) -> None:
    for name in names:
        edges = getattr(cfg, name)
        if len(edges) != 2 or not edges[0] < edges[1]:
            raise ConfigError(f"{name} must be two increasing numbers")


def _join(setup: pd.Series, *parts: pd.Series) -> pd.Series:
    sig = parts[0].astype(str)
    for p in parts[1:]:
        sig = sig + "|" + p.astype(str)
    return sig.where(setup, None)


def _first_in_day(cond: pd.Series, day: pd.Series) -> pd.Series:
    """True on the first bar of each session where ``cond`` holds."""
    c = cond.fillna(False).astype(bool)
    return c & (c.groupby(day).cumsum() == 1)


# ------------------------------------------------------------------ ORB


@dataclass(frozen=True)
class OrbConfig:
    entry_start: time = time(10, 0)
    entry_end: time = time(12, 0)
    flatten_at: time = time(15, 50)
    limit_offset_pct: float = 0.001
    range_minutes: int = 30
    sma_trend: int = 50
    max_range_daily_atr: float = 0.8  # skip days whose opening range is already huge
    max_stop_atr: float = 3.0  # stop at the range low, but at most 3 bar-ATRs away
    breakeven_at_r: float = 1.0
    trail_atr: float = 2.0
    min_price: float = 10.0
    width_edges: tuple[float, float] = (0.25, 0.5)  # range / daily ATR
    gap_edges: tuple[float, float] = (-0.003, 0.003)

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if self.range_minutes < 5 or self.range_minutes % 5:
            raise ConfigError("range_minutes must be a positive multiple of 5")
        if min(self.max_stop_atr, self.trail_atr, self.breakeven_at_r) <= 0:
            raise ConfigError("max_stop_atr, trail_atr and breakeven_at_r must be > 0")
        _check_edges(self, "width_edges", "gap_edges")


class OrbBreakout(IntradayStrategy):
    name: ClassVar[str] = "orb_breakout"
    description: ClassVar[str] = (
        "First close above the 30-minute opening range in a daily uptrend; stop at the "
        "range low, breakeven at +1R, then a 2 ATR trail; flat at 15:50."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "range_minutes", "max_range_daily_atr", "max_stop_atr", "trail_atr",
    )  # fmt: skip
    config: OrbConfig

    @classmethod
    def default_config(cls) -> OrbConfig:
        return OrbConfig()

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily, cfg.sma_trend)
        in_range = f["minute"] < minute_of_day(time(9, 30)) + cfg.range_minutes
        f["or_high"] = f["high"].where(in_range).groupby(f["day"]).transform("max")
        f["or_low"] = f["low"].where(in_range).groupby(f["day"]).transform("min")
        complete = in_range.astype(int).groupby(f["day"]).transform("sum")
        # The range is only known once its last bar closed: never before that bar.
        f.loc[in_range, ["or_high", "or_low"]] = np.nan
        f["range_ok_bars"] = complete >= cfg.range_minutes // self.bar_minutes  # no gaps
        width = (f["or_high"] - f["or_low"]) / f["daily_atr"]
        gap = f["day_open"] / f["prev_close"] - 1
        above = f["close"] > f["or_high"]
        cond = (
            above
            & f["range_ok_bars"]
            & f["regime_ok"]
            & (f["prev_close"] > f["prev_sma"])
            & (width < cfg.max_range_daily_atr)
            & (f["close"] > cfg.min_price)
            & f["atr"].notna()
        )
        # The first break of the day only (one entry per symbol per session).
        f["setup"] = _first_in_day(above.where(~in_range, False), f["day"]) & cond
        f["signature"] = _join(
            f["setup"],
            bucket_series(width, cfg.width_edges, _WIDTH),
            bucket_series(gap, cfg.gap_edges, _GAP),
        )
        f["score"] = -width
        return f

    def initial_stop(self, entry, bar):
        return atr_floor_stop(entry, bar["or_low"], bar["atr"], self.config.max_stop_atr)

    def on_bar(self, pos: IntradayPosition, bar: Mapping[str, float]):
        cfg = self.config
        close, atr = bar["close"], bar["atr"]
        require_finite(close=close, atr=atr)
        stop = pos.stop
        if pos.r_multiple(close) >= cfg.breakeven_at_r:
            stop = max(stop, pos.entry_price, floor_cents(close - cfg.trail_atr * atr))
        return None, stop


# ------------------------------------------------------------------ gap fill


@dataclass(frozen=True)
class GapFillConfig:
    entry_start: time = time(9, 45)
    entry_end: time = time(11, 0)
    flatten_at: time = time(15, 50)
    limit_offset_pct: float = 0.001
    gap_min: float = 0.005
    gap_max: float = 0.03
    sma_trend: int = 200
    stop_buffer_atr: float = 0.1  # below the day's low so far
    max_stop_atr: float = 3.0
    min_price: float = 10.0
    gap_edges: tuple[float, float] = (0.01, 0.02)
    trend_edges: tuple[float, float] = (0.03, 0.08)  # prev close / SMA200 - 1

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if not 0 < self.gap_min < self.gap_max < 0.2:
            raise ConfigError("need 0 < gap_min < gap_max < 0.2")
        if self.max_stop_atr <= 0 or self.stop_buffer_atr < 0:
            raise ConfigError("max_stop_atr must be > 0 and stop_buffer_atr >= 0")
        _check_edges(self, "gap_edges", "trend_edges")


class GapFill(IntradayStrategy):
    name: ClassVar[str] = "gap_fill"
    description: ClassVar[str] = (
        "Gap down 0.5-3% in a daily uptrend, then a 5-minute close back above the open; "
        "sell when yesterday's close is reached, stop under the day's low; flat 15:50."
    )
    stability_params: ClassVar[tuple[str, ...]] = ("gap_min", "gap_max", "max_stop_atr")
    config: GapFillConfig

    @classmethod
    def default_config(cls) -> GapFillConfig:
        return GapFillConfig()

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily, sma_trend=50,
                         sma_regime=cfg.sma_trend)  # fmt: skip
        gap = 1 - f["day_open"] / f["prev_close"]  # positive = gap down
        reclaim = f["close"] > f["day_open"]
        cond = (
            (gap >= cfg.gap_min)
            & (gap <= cfg.gap_max)
            & (f["close"] < f["prev_close"])  # the gap is still open
            & f["regime_ok"]
            & (f["prev_close"] > f["prev_sma_long"])
            & (f["close"] > cfg.min_price)
            & f["atr"].notna()
        )
        f["setup"] = _first_in_day(reclaim & (f["bar_no"] > 0), f["day"]) & cond
        trend = f["prev_close"] / f["prev_sma_long"] - 1
        f["signature"] = _join(
            f["setup"],
            bucket_series(gap, cfg.gap_edges, _SIZE),
            bucket_series(trend, cfg.trend_edges, _TREND),
        )
        f["score"] = (f["prev_close"] - f["close"]) / f["atr"]  # room left to the target
        return f

    def initial_stop(self, entry, bar):
        cfg = self.config
        level = bar["day_low"] - cfg.stop_buffer_atr * bar["atr"]
        return atr_floor_stop(entry, level, bar["atr"], cfg.max_stop_atr)

    def on_bar(self, pos, bar):
        close, target = bar["close"], bar["prev_close"]
        require_finite(close=close, prev_close=target)
        return ("gap_filled" if close >= target else None), pos.stop


# ------------------------------------------------------------------ reversion


@dataclass(frozen=True)
class ReversionConfig:
    entry_start: time = time(10, 30)
    entry_end: time = time(14, 30)
    flatten_at: time = time(15, 50)
    limit_offset_pct: float = 0.001
    rsi_below: float = 5.0
    stretch_atr: float = 1.5  # close at least this many ATRs under the EMA
    stop_atr: float = 2.5
    exit_rsi_above: float = 70.0
    time_stop_bars: int = 12
    min_price: float = 10.0
    depth_edges: tuple[float, float] = (2.0, 3.0)  # (EMA - close) / ATR
    tod_edges: tuple[float, float] = (12 * 60, 13 * 60 + 30)  # minute of day

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if not 0 < self.rsi_below < self.exit_rsi_above < 100:
            raise ConfigError("need 0 < rsi_below < exit_rsi_above < 100")
        if self.stretch_atr <= 0 or self.stop_atr <= 0 or self.time_stop_bars < 1:
            raise ConfigError("stretch_atr, stop_atr > 0 and time_stop_bars >= 1")
        _check_edges(self, "depth_edges", "tod_edges")


class IntradayReversion(IntradayStrategy):
    name: ClassVar[str] = "intraday_reversion"
    description: ClassVar[str] = (
        "RSI(2) under 5 and 1.5 ATR below the 20-bar EMA in a daily uptrend; sell on a "
        "close above the EMA, RSI(2) over 70 or after an hour; flat 15:50."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "rsi_below", "stretch_atr", "stop_atr", "time_stop_bars",
    )  # fmt: skip
    config: ReversionConfig

    @classmethod
    def default_config(cls) -> ReversionConfig:
        return ReversionConfig()

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily, sma_trend=50)
        depth = (f["ema"] - f["close"]) / f["atr"]
        cond = (
            (f["rsi2"] < cfg.rsi_below)
            & (depth >= cfg.stretch_atr)
            & f["regime_ok"]
            & (f["prev_close"] > f["prev_sma"])
            & (f["prev_close"] > f["prev_sma_long"])
            & (f["close"] > cfg.min_price)
        )
        f["setup"] = cond
        f["signature"] = _join(
            f["setup"],
            bucket_series(depth, cfg.depth_edges, _DEPTH),
            bucket_series(f["minute"].astype("float64"), cfg.tod_edges, _TOD),
        )
        f["score"] = depth
        return f

    def initial_stop(self, entry, bar):
        require_finite(atr=bar["atr"])
        if not bar["atr"] > 0:
            raise ValueError("ATR must be positive")
        return floor_cents(entry - self.config.stop_atr * bar["atr"])

    def on_bar(self, pos, bar):
        cfg = self.config
        close, ema_, rsi2 = bar["close"], bar["ema"], bar["rsi2"]
        require_finite(close=close, ema=ema_, rsi2=rsi2)
        if close > ema_:
            return "mean_reached", pos.stop
        if rsi2 > cfg.exit_rsi_above:
            return "rsi_exit", pos.stop
        if pos.bars_held >= cfg.time_stop_bars:
            return "time_stop", pos.stop
        return None, pos.stop


# ------------------------------------------------------------------ late momentum


@dataclass(frozen=True)
class LateMomentumConfig:
    entry_start: time = time(15, 25)
    entry_end: time = time(15, 30)
    flatten_at: time = time(15, 55)
    limit_offset_pct: float = 0.001
    first_minutes: int = 30
    min_first_return: float = 0.0025  # previous close -> 10:00
    stop_atr: float = 3.0
    first_edges: tuple[float, float] = (0.005, 0.01)

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if self.min_first_return <= 0 or self.stop_atr <= 0:
            raise ConfigError("min_first_return and stop_atr must be > 0")
        _check_edges(self, "first_edges")


class LateMomentum(IntradayStrategy):
    name: ClassVar[str] = "late_momentum"
    description: ClassVar[str] = (
        "SPY/QQQ: when the first half hour (from yesterday's close) is up over 0.25%, "
        "buy at 15:30 and sell at 15:55 (intraday momentum, Gao et al. 2018)."
    )
    symbols: ClassVar[tuple[str, ...] | None] = ("SPY", "QQQ")
    stability_params: ClassVar[tuple[str, ...]] = ("min_first_return", "stop_atr")
    config: LateMomentumConfig

    @classmethod
    def default_config(cls) -> LateMomentumConfig:
        return LateMomentumConfig()

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily)
        cut = minute_of_day(time(9, 30)) + cfg.first_minutes
        last_first = f["minute"] + self.bar_minutes == cut
        price = f["close"].where(last_first).groupby(f["day"]).transform("max")
        f["first_close"] = price.where(f["minute"] + self.bar_minutes >= cut)
        first = f["first_close"] / f["prev_close"] - 1
        mid = f["close"] / f["first_close"] - 1
        f["setup"] = (first > cfg.min_first_return) & f["regime_ok"] & f["atr"].notna()
        f["signature"] = _join(
            f["setup"],
            bucket_series(first, cfg.first_edges, _SIZE),
            pd.Series(np.where(mid > 0, "mid_up", "mid_down"), index=f.index),
        )
        f["score"] = first
        return f

    def initial_stop(self, entry, bar):
        require_finite(atr=bar["atr"])
        if not bar["atr"] > 0:
            raise ValueError("ATR must be positive")
        return floor_cents(entry - self.config.stop_atr * bar["atr"])

    def on_bar(self, pos, bar):
        return None, pos.stop


def intraday_registry() -> dict[str, type[IntradayStrategy]]:
    from bot.intraday.auction import IbFailure, VaRejection, VaRotation
    from bot.intraday.published import NoiseBreakout, OrbInPlay, VwapTrend

    classes = (OrbBreakout, GapFill, IntradayReversion, LateMomentum,
               VaRotation, VaRejection, IbFailure, NoiseBreakout, VwapTrend,
               OrbInPlay)  # fmt: skip
    return {cls.name: cls for cls in classes}


def get_intraday_strategy(name: str, config: object = None) -> IntradayStrategy:
    classes = intraday_registry()
    if name not in classes:
        raise ValueError(
            f"unknown intraday strategy {name!r}; available: {', '.join(sorted(classes))}"
        )
    return classes[name](config)


DEFAULT_INTRADAY_STRATEGY = "orb_breakout"
