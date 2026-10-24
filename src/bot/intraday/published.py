# Author: Kai0421
"""Three published intraday strategies, long side only (docs/17-intraday.md).

Chosen (2026-10-07) as the best-documented intraday results with costs included and
rules simple enough to reproduce on 5-minute bars:

- ``noise_breakout`` -- Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective
  Intraday Momentum Strategy for S&P500 ETF (SPY)", SFI 24-97: 19.6%/yr, Sharpe 1.33,
  2007-2024, long and short with up to 4x leverage. The "noise area" around the open is
  the average absolute move from the open at the same time of day over the last 14
  sessions. At each half hour from 10:00, buy when price closes above the top of the
  area; sell at a half hour when price closes below max(area top, VWAP); out by the close.
- ``vwap_trend`` -- Zarattini & Aziz (2023), "VWAP: The Holy Grail for Day Trading
  Systems", SSRN 4631351: QQQ 2018-2023, Sharpe 2.1, long above VWAP and short below on
  1-minute bars. Here: QQQ/SPY, buy the first 5-minute close above VWAP, sell on a close
  below it. One entry per session (the bot's rule), so no re-entries after an exit.
- ``orb_in_play`` -- Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy
  For The U.S. Equity Market", SFI 24-98: 5-minute opening range breakout on the top 20
  "stocks in play" (first-bar relative volume), 2016-2023, Sharpe 2.81 net of commissions.
  Buy the second bar's open after an up first bar with relative volume >= 1, stop 10% of
  the daily ATR under the entry, hold to the close. The paper's edge came from ~7,000
  stocks including small caps with news; our universe is large caps only.

Differences that apply to all three: long only, no leverage (sizing is the bot's 0.25%
risk / 5% per position), flat at 15:55, 10 bps round-trip costs (the papers charged
about $0.0035-0.0045 per share, well under 1 bp on SPY/QQQ).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import ClassVar

import numpy as np
import pandas as pd

from bot.config import ConfigError
from bot.intraday.base import (
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

__all__ = ["NoiseBreakout", "OrbInPlay", "VwapTrend", "session_vwap"]

_GAP = ("down", "flat", "up")
_TOD = ("morning", "midday", "afternoon")
_RVOL = ("normal", "high", "extreme")


def session_vwap(f: pd.DataFrame) -> pd.Series:
    """Running volume-weighted average of the typical price, reset every session."""
    typical = (f["high"] + f["low"] + f["close"]) / 3
    g = f["day"]
    pv = (typical * f["volume"]).groupby(g).cumsum()
    vol = f["volume"].groupby(g).cumsum()
    return (pv / vol.where(vol > 0)).fillna(typical)


def _join(setup: pd.Series, *parts: pd.Series) -> pd.Series:
    def text(p: pd.Series) -> pd.Series:
        return p.astype(object).where(p.notna(), "na").astype(str)  # unknown bucket: "na"

    sig = text(parts[0])
    for p in parts[1:]:
        sig = sig + "|" + text(p)
    return sig.where(setup, None)


def _first_in_day(cond: pd.Series, day: pd.Series) -> pd.Series:
    c = cond.fillna(False).astype(bool)
    return c & (c.groupby(day).cumsum() == 1)


def _gap(f: pd.DataFrame) -> pd.Series:
    return f["day_open"] / f["prev_close"] - 1


# ------------------------------------------------------------------ noise area


@dataclass(frozen=True)
class NoiseConfig:
    entry_start: time = time(9, 55)  # first check is the bar ending 10:00
    entry_end: time = time(15, 30)
    flatten_at: time = time(15, 55)
    limit_offset_pct: float = 0.001
    lookback_days: int = 14
    band_mult: float = 1.0
    check_minutes: int = 30  # signals and exits only on bars ending on the half hour
    stop_buffer_atr: float = 0.5  # protective stop under max(area top, VWAP)
    max_stop_atr: float = 4.0
    gap_edges: tuple[float, float] = (-0.003, 0.003)
    tod_edges: tuple[float, float] = (11 * 60 + 30, 14 * 60)

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if self.lookback_days < 2 or self.band_mult <= 0:
            raise ConfigError("need lookback_days >= 2 and band_mult > 0")
        if self.check_minutes < 5 or self.check_minutes % 5:
            raise ConfigError("check_minutes must be a positive multiple of 5")
        if self.max_stop_atr <= 0 or self.stop_buffer_atr < 0:
            raise ConfigError("max_stop_atr must be > 0 and stop_buffer_atr >= 0")


class NoiseBreakout(IntradayStrategy):
    name: ClassVar[str] = "noise_breakout"
    description: ClassVar[str] = (
        "SPY/QQQ: at each half hour buy a close above the 14-day 'noise area' around the "
        "open; sell at a half hour below max(area top, VWAP); flat 15:55 (Zarattini 2024)."
    )
    symbols: ClassVar[tuple[str, ...] | None] = ("SPY", "QQQ")
    stability_params: ClassVar[tuple[str, ...]] = ("lookback_days", "band_mult")
    config: NoiseConfig

    @classmethod
    def default_config(cls) -> NoiseConfig:
        return NoiseConfig()

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily)
        f["vwap"] = session_vwap(f)
        bar_end = f["minute"] + self.bar_minutes
        move = (f["close"] / f["day_open"] - 1).abs()
        # sigma at this time of day: mean move over the previous `lookback_days` sessions
        sigma = move.groupby(f["minute"]).transform(
            lambda s: s.shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).mean()
        )
        base = np.fmax(f["day_open"], f["prev_close"])  # open alone if no daily bar
        f["upper"] = base * (1 + cfg.band_mult * sigma)
        f["check"] = (bar_end % cfg.check_minutes == 0) & (bar_end >= minute_of_day(time(10, 0)))
        cond = f["check"] & (f["close"] > f["upper"]) & f["atr"].notna()
        f["setup"] = _first_in_day(cond, f["day"])
        f["signature"] = _join(
            f["setup"],
            bucket_series(bar_end.astype("float64"), cfg.tod_edges, _TOD),
            bucket_series(_gap(f), cfg.gap_edges, _GAP),
        )
        f["score"] = f["close"] / f["upper"] - 1
        return f

    def initial_stop(self, entry, bar):
        cfg = self.config
        require_finite(upper=bar["upper"], vwap=bar["vwap"], atr=bar["atr"])
        level = min(max(bar["upper"], bar["vwap"]), entry) - cfg.stop_buffer_atr * bar["atr"]
        return atr_floor_stop(entry, level, bar["atr"], cfg.max_stop_atr)

    def on_bar(self, pos, bar):
        if not bar["check"]:
            return None, pos.stop
        require_finite(close=bar["close"], upper=bar["upper"], vwap=bar["vwap"])
        trail = max(bar["upper"], bar["vwap"])
        return ("below_trail" if bar["close"] < trail else None), pos.stop


# ------------------------------------------------------------------ VWAP trend


@dataclass(frozen=True)
class VwapConfig:
    entry_start: time = time(9, 35)  # the bar ending 09:40 is the first signal
    entry_end: time = time(15, 0)
    flatten_at: time = time(15, 55)
    limit_offset_pct: float = 0.001
    stop_buffer_atr: float = 0.5
    max_stop_atr: float = 4.0
    gap_edges: tuple[float, float] = (-0.003, 0.003)
    tod_edges: tuple[float, float] = (10 * 60 + 30, 13 * 60)

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if self.max_stop_atr <= 0 or self.stop_buffer_atr < 0:
            raise ConfigError("max_stop_atr must be > 0 and stop_buffer_atr >= 0")


class VwapTrend(IntradayStrategy):
    name: ClassVar[str] = "vwap_trend"
    description: ClassVar[str] = (
        "QQQ/SPY: buy the first 5-minute close above the session VWAP, sell on a close "
        "below it; flat 15:55 (Zarattini & Aziz 2023, long side)."
    )
    symbols: ClassVar[tuple[str, ...] | None] = ("QQQ", "SPY")
    stability_params: ClassVar[tuple[str, ...]] = ("stop_buffer_atr",)
    config: VwapConfig

    @classmethod
    def default_config(cls) -> VwapConfig:
        return VwapConfig()

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily)
        f["vwap"] = session_vwap(f)
        cond = (f["close"] > f["vwap"]) & (f["bar_no"] > 0) & f["atr"].notna()
        f["setup"] = _first_in_day(cond, f["day"])
        bar_end = (f["minute"] + self.bar_minutes).astype("float64")
        f["signature"] = _join(
            f["setup"],
            bucket_series(bar_end, cfg.tod_edges, _TOD),
            bucket_series(_gap(f), cfg.gap_edges, _GAP),
        )
        f["score"] = f["close"] / f["vwap"] - 1
        return f

    def initial_stop(self, entry, bar):
        cfg = self.config
        require_finite(vwap=bar["vwap"], atr=bar["atr"])
        level = min(bar["vwap"], entry) - cfg.stop_buffer_atr * bar["atr"]
        return atr_floor_stop(entry, level, bar["atr"], cfg.max_stop_atr)

    def on_bar(self, pos, bar):
        require_finite(close=bar["close"], vwap=bar["vwap"])
        return ("below_vwap" if bar["close"] < bar["vwap"] else None), pos.stop


# ------------------------------------------------------------------ ORB in play


@dataclass(frozen=True)
class OrbInPlayConfig:
    entry_start: time = time(9, 31)  # setup on the first bar (ends 09:35)
    entry_end: time = time(9, 40)
    flatten_at: time = time(15, 55)
    limit_offset_pct: float = 0.001
    rvol_days: int = 14
    min_rvol: float = 1.0
    stop_daily_atr: float = 0.10  # stop 10% of the daily ATR under the entry
    min_price: float = 5.0
    min_daily_atr: float = 0.5  # dollars
    rvol_edges: tuple[float, float] = (2.0, 4.0)
    gap_edges: tuple[float, float] = (-0.005, 0.005)

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if self.rvol_days < 2 or self.min_rvol <= 0 or not 0 < self.stop_daily_atr < 1:
            raise ConfigError("need rvol_days >= 2, min_rvol > 0, 0 < stop_daily_atr < 1")


class OrbInPlay(IntradayStrategy):
    name: ClassVar[str] = "orb_in_play"
    description: ClassVar[str] = (
        "Up first 5-minute bar on at least normal relative volume: buy the next bar, stop "
        "10% of the daily ATR below, hold to 15:55 (Zarattini, Barbon & Aziz 2024, long)."
    )
    stability_params: ClassVar[tuple[str, ...]] = ("min_rvol", "stop_daily_atr")
    config: OrbInPlayConfig

    @classmethod
    def default_config(cls) -> OrbInPlayConfig:
        return OrbInPlayConfig()

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily)
        first = f["bar_no"] == 0
        first_vol = f["volume"].where(first)
        # average opening-bar volume over the previous sessions (first bars only)
        avg = first_vol.dropna().shift(1).rolling(cfg.rvol_days, min_periods=cfg.rvol_days).mean()
        f["rvol"] = first_vol / avg.reindex(f.index)
        cond = (
            first
            & (f["close"] > f["open"])
            & (f["rvol"] >= cfg.min_rvol)
            & (f["close"] > cfg.min_price)
            & (f["daily_atr"] > cfg.min_daily_atr)
            & f["atr"].notna()
        )
        f["setup"] = cond.fillna(False)
        f["signature"] = _join(
            f["setup"],
            bucket_series(f["rvol"], cfg.rvol_edges, _RVOL),
            bucket_series(_gap(f), cfg.gap_edges, _GAP),
        )
        f["score"] = f["rvol"]
        return f

    def initial_stop(self, entry, bar):
        require_finite(daily_atr=bar["daily_atr"])
        if not bar["daily_atr"] > 0:
            raise ValueError("daily ATR must be positive")
        return floor_cents(entry - self.config.stop_daily_atr * bar["daily_atr"])

    def on_bar(self, pos, bar):
        return None, pos.stop
