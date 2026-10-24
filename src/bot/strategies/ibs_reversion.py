# Author: Kai0421
"""Internal Bar Strength (IBS) reversion: buy a close near the day's low in an uptrend.

IBS = (close - low) / (high - low). Pagonidis (2013) found US equity ETFs closing in the
bottom fifth of their range rose 0.35% on average the next day, against -0.13% after
closing in the top fifth, with the effect strongest on wide-range days
(docs/14-research-strategies.md). Long only, daily bars:

- **Regime:** SPY closes above its 200-day SMA.
- **Entry:** close > SMA(200), IBS < ``ibs_entry_below``, close below the previous close,
  price and liquidity filters. Lowest IBS ranks first.
- **Stop:** entry - ``stop_atr_mult`` x ATR(14), fixed; the hold is a few days.
- **Exits (on the close, executed next open):** close above the previous day's high
  ("prev_high_exit"), IBS above ``ibs_exit_above`` ("ibs_exit"), or after
  ``time_stop_days`` ("time_stop").
- **Signature:** SPY volatility | IBS depth | day range versus ATR.

Parameters are hypotheses until backtested (memory/decisions.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from bot.config import ConfigError
from bot.strategies.base import ExitAction, PositionState, Strategy, bucket_series
from bot.strategies.common import (
    add_common,
    atr_stop,
    base_filters,
    finite_bar,
    internal_bar_strength,
    join_signature,
    ohlcv_frame,
    spy_context,
)

_DEPTH = ("extreme", "deep", "mild")
_RANGE = ("narrow", "normal", "wide")


@dataclass(frozen=True)
class IbsConfig:
    sma_trend: int = 200
    ibs_entry_below: float = 0.2
    ibs_exit_above: float = 0.8
    atr_period: int = 14
    stop_atr_mult: float = 3.0
    time_stop_days: int = 5
    min_price: float = 10.0
    min_avg_dollar_volume: float = 50_000_000
    avg_volume_days: int = 20
    limit_offset_pct: float = 0.002
    spy_vol_edges: tuple[float, float] = (0.01, 0.02)  # SPY ATR / close
    ibs_depth_edges: tuple[float, float] = (0.05, 0.12)  # IBS on the signal day
    range_edges: tuple[float, float] = (0.8, 1.3)  # (high - low) / ATR on the signal day

    def __post_init__(self) -> None:
        if self.sma_trend < 2 or self.atr_period < 1 or self.avg_volume_days < 1:
            raise ConfigError("sma_trend must be >= 2; atr_period, avg_volume_days >= 1")
        if self.stop_atr_mult <= 0:
            raise ConfigError("stop_atr_mult must be > 0: every position needs a stop")
        if not 0 < self.ibs_entry_below < self.ibs_exit_above <= 1:
            raise ConfigError("need 0 < ibs_entry_below < ibs_exit_above <= 1")
        if self.time_stop_days < 1:
            raise ConfigError("time_stop_days must be >= 1")
        for name in ("spy_vol_edges", "ibs_depth_edges", "range_edges"):
            edges = getattr(self, name)
            if len(edges) != 2 or not edges[0] < edges[1]:
                raise ConfigError(f"{name} must be two increasing numbers")


class IbsReversion(Strategy):
    name: ClassVar[str] = "ibs_reversion"
    description: ClassVar[str] = (
        "Buy a close near the day's low (IBS) in an uptrend; sell on a close above the "
        "previous high, a close near the high, or after 5 days."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "sma_trend",
        "ibs_entry_below",
        "ibs_exit_above",
        "stop_atr_mult",
        "time_stop_days",
    )

    config: IbsConfig

    @classmethod
    def default_config(cls) -> IbsConfig:
        return IbsConfig()

    def features(self, bars: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        f = add_common(ohlcv_frame(bars), cfg.sma_trend, cfg.atr_period, cfg.avg_volume_days)
        f["ibs"] = internal_bar_strength(f)
        f["prev_close"] = f["close"].shift(1)
        f["prev_high"] = f["high"].shift(1)
        return f

    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        cfg = self.config
        regime_ok, spy_vol = spy_context(features[benchmark], cfg.spy_vol_edges)
        tables = {}
        for symbol, f in features.items():
            needed = ["sma_trend", "atr", "avg_dollar_volume", "prev_close"]
            t = base_filters(f, regime_ok, needed, cfg.min_price, cfg.min_avg_dollar_volume)
            t["uptrend"] = f["close"] > f["sma_trend"]
            t["low_close"] = f["ibs"] < cfg.ibs_entry_below
            t["down_day"] = f["close"] < f["prev_close"]
            t["setup"] = t.all(axis=1)
            t["signature"] = join_signature(
                t,
                [
                    spy_vol.reindex(f.index),
                    bucket_series(f["ibs"], cfg.ibs_depth_edges, _DEPTH),
                    bucket_series((f["high"] - f["low"]) / f["atr"], cfg.range_edges, _RANGE),
                ],
            )
            t["score"] = -f["ibs"]  # closest to the low first
            tables[symbol] = t
        return tables

    def initial_stop(self, entry: float, atr: float) -> float:
        return atr_stop(entry, atr, self.config.stop_atr_mult)

    def on_close(
        self, pos: PositionState, bar: Mapping[str, float]
    ) -> tuple[list[ExitAction], float]:
        close, prev_high, ibs = finite_bar(bar, "close", "prev_high", "ibs")
        if close > prev_high:
            return [ExitAction("prev_high_exit", pos.shares)], pos.stop
        if ibs > self.config.ibs_exit_above:
            return [ExitAction("ibs_exit", pos.shares)], pos.stop
        if pos.days_held >= self.config.time_stop_days:
            return [ExitAction("time_stop", pos.shares)], pos.stop
        return [], pos.stop
