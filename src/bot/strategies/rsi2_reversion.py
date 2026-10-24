# Author: Kai0421
"""RSI(2) mean reversion: buy a sharp short-term dip in a long-term uptrend, sell the bounce.

A classic short-hold swing setup, here as a second strategy to compare against Trend
Pullback. Long only, daily bars:

- **Regime:** SPY closes above its 200-day SMA (no entries otherwise).
- **Entry:** close > SMA(200), RSI(2) < ``rsi_entry_below``, price and liquidity filters.
  Entry at the next open like every strategy; lowest RSI(2) ranks first.
- **Stop:** entry - ``stop_atr_mult`` x ATR(14), fixed (no trailing; the hold is short).
- **Exits (on the close, executed next open):** close above the SMA(5) -> sell everything
  ("sma_exit"); after ``time_stop_days`` -> sell everything ("time_stop").
- **Signature:** SPY volatility | dip depth | distance above the SMA(200).

Parameters are hypotheses until backtested (memory/decisions.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from bot.config import ConfigError
from bot.indicators import atr, rsi, sma
from bot.strategies.base import (
    OHLCV,
    ExitAction,
    PositionState,
    Strategy,
    bucket_series,
    floor_cents,
    require_finite,
)

_VOL = ("low", "mid", "high")
_DEPTH = ("extreme", "deep", "mild")
_DISTANCE = ("near", "mid", "far")


@dataclass(frozen=True)
class Rsi2Config:
    sma_trend: int = 200
    sma_exit: int = 5
    rsi_period: int = 2
    rsi_entry_below: float = 10.0
    atr_period: int = 14
    stop_atr_mult: float = 2.5
    time_stop_days: int = 10
    min_price: float = 10.0
    min_avg_dollar_volume: float = 50_000_000
    avg_volume_days: int = 20
    limit_offset_pct: float = 0.002
    spy_vol_edges: tuple[float, float] = (0.01, 0.02)  # SPY ATR / close
    rsi_depth_edges: tuple[float, float] = (2.0, 5.0)  # RSI(2) on the signal day
    trend_distance_edges: tuple[float, float] = (0.05, 0.15)  # close / SMA200 - 1

    def __post_init__(self) -> None:
        if not 2 <= self.sma_exit < self.sma_trend:
            raise ConfigError("sma_exit must be >= 2 and shorter than sma_trend")
        if self.stop_atr_mult <= 0:
            raise ConfigError("stop_atr_mult must be > 0: every position needs a stop")
        if self.time_stop_days < 1 or not 0 < self.rsi_entry_below < 100:
            raise ConfigError("time_stop_days must be >= 1 and rsi_entry_below in (0, 100)")
        for name in ("spy_vol_edges", "rsi_depth_edges", "trend_distance_edges"):
            edges = getattr(self, name)
            if len(edges) != 2 or not edges[0] < edges[1]:
                raise ConfigError(f"{name} must be two increasing numbers")


class Rsi2Reversion(Strategy):
    name: ClassVar[str] = "rsi2_reversion"
    description: ClassVar[str] = (
        "Buy RSI(2) dips above the 200-day SMA, sell on a close above the 5-day SMA."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "sma_trend",
        "sma_exit",
        "rsi_entry_below",
        "stop_atr_mult",
        "time_stop_days",
    )

    config: Rsi2Config

    @classmethod
    def default_config(cls) -> Rsi2Config:
        return Rsi2Config()

    def features(self, bars: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        missing = [c for c in OHLCV if c not in bars.columns]
        if missing:
            raise ValueError(f"bars are missing columns: {missing}")
        if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
            raise ValueError("bars index must be sorted ascending with no duplicates")
        f = bars.loc[:, list(OHLCV)].astype("float64")
        close = f["close"]
        f["sma_trend"] = sma(close, cfg.sma_trend)
        f["sma_exit"] = sma(close, cfg.sma_exit)
        f["rsi"] = rsi(close, cfg.rsi_period)
        f["atr"] = atr(f["high"], f["low"], close, cfg.atr_period)
        f["avg_dollar_volume"] = (close * f["volume"]).rolling(cfg.avg_volume_days).mean()
        return f

    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        cfg = self.config
        spy = features[benchmark]
        spy_known = spy["sma_trend"].notna() & spy["atr"].notna()
        regime_ok = spy_known & (spy["close"] > spy["sma_trend"])
        spy_vol = bucket_series(spy["atr"] / spy["close"], cfg.spy_vol_edges, _VOL)

        tables = {}
        for symbol, f in features.items():
            close = f["close"]
            needed = ["sma_trend", "sma_exit", "rsi", "atr", "avg_dollar_volume"]
            t = pd.DataFrame(index=f.index)
            t["history_ok"] = f[needed].notna().all(axis=1)
            t["regime_ok"] = regime_ok.reindex(f.index).eq(True)
            t["price_ok"] = close > cfg.min_price
            t["volume_ok"] = f["avg_dollar_volume"] > cfg.min_avg_dollar_volume
            t["uptrend"] = close > f["sma_trend"]
            t["dip"] = f["rsi"] < cfg.rsi_entry_below
            t["setup"] = t.all(axis=1)
            parts = (
                spy_vol.reindex(f.index).astype(str),
                bucket_series(f["rsi"], cfg.rsi_depth_edges, _DEPTH).astype(str),
                bucket_series(
                    close / f["sma_trend"] - 1, cfg.trend_distance_edges, _DISTANCE
                ).astype(str),
            )
            t["signature"] = (parts[0] + "|" + parts[1] + "|" + parts[2]).where(t["setup"], None)
            t["score"] = -f["rsi"]  # deepest dip first
            tables[symbol] = t
        return tables

    def initial_stop(self, entry: float, atr: float) -> float:
        require_finite(atr=atr)
        if not atr > 0:
            raise ValueError(f"ATR must be a positive number (got {atr})")
        return floor_cents(entry - self.config.stop_atr_mult * atr)

    def on_close(
        self, pos: PositionState, bar: Mapping[str, float]
    ) -> tuple[list[ExitAction], float]:
        close, sma_exit = bar["close"], bar["sma_exit"]
        require_finite(close=close, sma_exit=sma_exit)
        if close > sma_exit:
            return [ExitAction("sma_exit", pos.shares)], pos.stop
        if pos.days_held >= self.config.time_stop_days:
            return [ExitAction("time_stop", pos.shares)], pos.stop
        return [], pos.stop
