# Author: Kai0421
"""Auction-geometry intraday strategies (Market Profile), long only.

Adapted from Alqaydi (2026), "Harvesting the Value Area: Auction Geometry as a Durable
Intraday Edge in Nasdaq-100 Futures" (SSRN 7546200). The paper trades NQ/MNQ futures
both ways; this bot may only buy US stocks and unleveraged ETFs, so only the long side
of each setup is kept and every symbol gets its own profile. The paper's order-flow
"absorption" filter is dropped: its own null test credits it with about 5% of the edge,
and it needs volume the free real-time feed doesn't have.

Reference levels come from the *previous* session's 5-minute bars (no look-ahead):

- ``poc``: the price bin that traded the most volume (each bar's volume is spread evenly
  over the bins between its low and high; bins are ``bin_pct`` of the session's open).
- ``val`` / ``vah``: the value area, grown outward from the POC (bigger neighbour first)
  until it holds ``va_share`` (70%) of the session's volume.
- ``ib_low`` / ``ib_high``: today's first-hour range, known once the 10:25 bar closes.

Strategies (paper section in brackets):

- ``va_rotation`` [5, Refined 80% rule]: the session opens inside yesterday's value area
  below the POC; buy the first up bar that closes between VAL and POC. Stop under VAL,
  target the POC.
- ``va_rejection`` [4, IARM]: price probes under yesterday's VAL, then an up bar closes
  back inside the value area within ``probe_bars``. Stop under the probe low, target POC.
- ``ib_failure`` [7, IB auction failure]: after 10:30 price probes under the first-hour
  low, then an up bar closes back inside it within ``probe_bars``. Stop under the probe
  low, target the IB midpoint.

Every setup needs reward >= ``min_rr`` x risk (paper: 1.2) and risk between
``min_risk_atr`` and ``max_risk_atr`` bar ATRs, so 10 bps of costs are not most of 1R.
The target sells at the next bar's open after a close at or above it. Entries 10:00 to
13:30 (paper's window); flat by 15:50.
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
    attach_daily,
    bucket_series,
    check_times,
    floor_cents,
    minute_of_day,
    session_features,
)
from bot.strategies.base import require_finite

__all__ = ["IbFailure", "VaRejection", "VaRotation", "initial_balance", "value_areas"]

_ROOM = ("near", "mid", "far")  # reward / risk at the signal bar


def _profile(low: np.ndarray, high: np.ndarray, vol: np.ndarray, step: float, share: float):
    """(poc, val, vah) of one session, or NaNs when it has no usable volume."""
    base = np.floor(low.min() / step)
    lo_bin = (np.floor(low / step) - base).astype(int)
    hi_bin = (np.floor(high / step) - base).astype(int)
    hist = np.zeros(int(hi_bin.max()) + 1)
    for a, b, v in zip(lo_bin, hi_bin, vol, strict=True):
        if v > 0:
            hist[a : b + 1] += v / (b - a + 1)
    total = hist.sum()
    if not total > 0:
        return np.nan, np.nan, np.nan
    poc = int(np.argmax(hist))
    lo = hi = poc
    covered = hist[poc]
    while covered < share * total:
        below = hist[lo - 1] if lo > 0 else -1.0
        above = hist[hi + 1] if hi + 1 < len(hist) else -1.0
        if above >= below:
            hi += 1
            covered += above
        else:
            lo -= 1
            covered += below
    return (base + poc + 0.5) * step, (base + lo) * step, (base + hi + 1) * step


def value_areas(f: pd.DataFrame, bin_pct: float = 0.0005, share: float = 0.70) -> pd.DataFrame:
    """Per bar: the *previous* session's ``poc``, ``val`` and ``vah`` (NaN on the first
    session). ``f`` needs ``day``, ``day_open``, ``low``, ``high`` and ``volume``."""
    rows = {}
    for day, g in f.groupby("day", sort=True):
        step = g["day_open"].iloc[0] * bin_pct
        if not step > 0:
            rows[day] = (np.nan, np.nan, np.nan)
            continue
        rows[day] = _profile(
            g["low"].to_numpy(), g["high"].to_numpy(), g["volume"].to_numpy(), step, share
        )
    levels = pd.DataFrame.from_dict(rows, orient="index", columns=["poc", "val", "vah"])
    prior = levels.shift(1)  # yesterday's levels for today's bars
    return prior.reindex(f["day"]).set_axis(f.index)


def initial_balance(f: pd.DataFrame, bar_minutes: int, minutes: int = 60) -> pd.DataFrame:
    """Per bar: today's first-hour ``ib_low`` / ``ib_high``, from the bar that completes
    the first hour onwards (NaN before it)."""
    cut = minute_of_day(time(9, 30)) + minutes
    inside = f["minute"] + bar_minutes <= cut
    lo = f["low"].where(inside).groupby(f["day"]).transform("min")
    hi = f["high"].where(inside).groupby(f["day"]).transform("max")
    known = f["minute"] + bar_minutes >= cut
    return pd.DataFrame({"ib_low": lo.where(known), "ib_high": hi.where(known)})


@dataclass(frozen=True)
class AuctionConfig:
    entry_start: time = time(10, 0)
    entry_end: time = time(13, 30)
    flatten_at: time = time(15, 50)
    limit_offset_pct: float = 0.001
    bin_pct: float = 0.0005  # profile bin, share of the session's open
    va_share: float = 0.70
    probe_bars: int = 3  # the probe must be this recent (bars, including this one)
    stop_buffer_atr: float = 0.1  # below the structural level
    min_risk_atr: float = 0.5
    max_risk_atr: float = 3.0
    min_rr: float = 1.2
    min_price: float = 10.0
    room_edges: tuple[float, float] = (2.0, 3.0)  # reward / risk buckets

    def __post_init__(self) -> None:
        check_times(self.entry_start, self.entry_end, self.flatten_at)
        if not 0 < self.bin_pct < 0.01 or not 0.5 <= self.va_share < 1:
            raise ConfigError("need 0 < bin_pct < 0.01 and 0.5 <= va_share < 1")
        if self.probe_bars < 1 or self.stop_buffer_atr < 0 or self.min_rr <= 0:
            raise ConfigError("need probe_bars >= 1, stop_buffer_atr >= 0, min_rr > 0")
        if not 0 < self.min_risk_atr < self.max_risk_atr:
            raise ConfigError("need 0 < min_risk_atr < max_risk_atr")
        if len(self.room_edges) != 2 or not self.room_edges[0] < self.room_edges[1]:
            raise ConfigError("room_edges must be two increasing numbers")


class _Auction(IntradayStrategy):
    """Shared plumbing: levels, the stop at ``stop_level``, the R:R gate and the exit at
    ``target``. Subclasses set ``trigger`` (bool), ``stop_level`` and ``target``."""

    stability_params: ClassVar[tuple[str, ...]] = ("min_rr", "probe_bars", "stop_buffer_atr")
    config: AuctionConfig

    @classmethod
    def default_config(cls) -> AuctionConfig:
        return AuctionConfig()

    def _levels(self, f: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def features(self, bars, daily, spy_daily):
        cfg = self.config
        f = attach_daily(session_features(bars), daily, spy_daily)
        f = self._levels(f, bars)
        stop = f["stop_level"] - cfg.stop_buffer_atr * f["atr"]
        risk = f["close"] - stop
        reward = f["target"] - f["close"]
        rr = reward / risk.where(risk > 0)
        cond = (
            f["trigger"].fillna(False).astype(bool)
            & (f["close"] > f["open"])  # an up bar confirms the rejection
            & (risk >= cfg.min_risk_atr * f["atr"])
            & (risk <= cfg.max_risk_atr * f["atr"])
            & (rr >= cfg.min_rr)
            & (f["close"] > cfg.min_price)
            & f["atr"].notna()
            & self._in_window(f)  # so a setup before the window doesn't use up the day
        )
        first = cond & (cond.groupby(f["day"]).cumsum() == 1)
        f["setup"] = first
        # the paper trades full size until 11:30, half size after
        early = f["minute"] + self.bar_minutes <= minute_of_day(time(11, 30))
        tod = pd.Series(np.where(early, "early", "late"), index=f.index)
        f["signature"] = (bucket_series(rr, cfg.room_edges, _ROOM) + "|" + tod).where(first, None)
        f["score"] = rr
        return f

    def _in_window(self, f: pd.DataFrame) -> pd.Series:
        end = f["minute"] + self.bar_minutes
        cfg = self.config
        return (end > minute_of_day(cfg.entry_start)) & (end <= minute_of_day(cfg.entry_end))

    def initial_stop(self, entry, bar):
        cfg = self.config
        require_finite(level=bar["stop_level"], atr=bar["atr"])
        return floor_cents(bar["stop_level"] - cfg.stop_buffer_atr * bar["atr"])

    def on_bar(self, pos, bar):
        close, target = bar["close"], bar["target"]
        require_finite(close=close, target=target)
        return ("target" if close >= target else None), pos.stop


def _probe_low(f: pd.DataFrame, n: int) -> pd.Series:
    """Lowest low of the last ``n`` bars of the same session (this bar included)."""
    return f["low"].groupby(f["day"]).transform(lambda s: s.rolling(n, min_periods=1).min())


class VaRotation(_Auction):
    name: ClassVar[str] = "va_rotation"
    description: ClassVar[str] = (
        "Open inside yesterday's value area below its POC; buy an up bar closing between "
        "VAL and POC, stop under VAL, sell at the POC (80% rule, long side); flat 15:50."
    )

    def _levels(self, f, bars):
        cfg = self.config
        f = f.join(value_areas(f.assign(volume=bars["volume"]), cfg.bin_pct, cfg.va_share))
        opened_inside = (f["day_open"] > f["val"]) & (f["day_open"] < f["poc"])
        held = f["day_low"] > f["val"] - cfg.stop_buffer_atr * f["atr"]  # never left
        f["trigger"] = opened_inside & held & (f["close"] >= f["val"]) & (f["close"] < f["poc"])
        f["stop_level"] = f["val"]
        f["target"] = f["poc"]
        return f


class VaRejection(_Auction):
    name: ClassVar[str] = "va_rejection"
    description: ClassVar[str] = (
        "A probe under yesterday's value-area low, then an up bar closing back inside; "
        "stop under the probe low, sell at the POC (IARM, long side); flat 15:50."
    )

    def _levels(self, f, bars):
        cfg = self.config
        f = f.join(value_areas(f.assign(volume=bars["volume"]), cfg.bin_pct, cfg.va_share))
        probe = _probe_low(f, cfg.probe_bars)
        f["trigger"] = (probe < f["val"]) & (f["close"] >= f["val"]) & (f["close"] < f["poc"])
        f["stop_level"] = probe
        f["target"] = f["poc"]
        return f


class IbFailure(_Auction):
    name: ClassVar[str] = "ib_failure"
    description: ClassVar[str] = (
        "After 10:30 a probe under the first-hour low, then an up bar closing back "
        "inside; stop under the probe low, sell at the range midpoint; flat 15:50."
    )

    @classmethod
    def default_config(cls) -> AuctionConfig:
        return AuctionConfig(entry_start=time(10, 30))

    def _levels(self, f, bars):
        cfg = self.config
        f = f.join(initial_balance(f, self.bar_minutes))
        probe = _probe_low(f, cfg.probe_bars)
        mid = (f["ib_low"] + f["ib_high"]) / 2
        # the probe must come after the first hour, not be the hour's own low
        first_probe_bar = f["minute"] - self.bar_minutes * (cfg.probe_bars - 1)
        after = first_probe_bar >= minute_of_day(time(10, 30))
        inside = (f["close"] >= f["ib_low"]) & (f["close"] < mid)
        f["trigger"] = after & (probe < f["ib_low"]) & inside
        f["stop_level"] = probe
        f["target"] = mid
        return f
