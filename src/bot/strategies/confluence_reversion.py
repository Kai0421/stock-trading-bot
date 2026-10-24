# Author: Kai0421
"""Confluence reversion: buy a dip when several independent oversold signals agree.

Four oversold votes, each a documented short-term reversal signal on its own
(docs/14-research-strategies.md):

1. RSI(2) below ``rsi_below`` (Connors).
2. IBS below ``ibs_below`` (Pagonidis 2013).
3. ``streak_days`` or more lower closes in a row.
4. Close below the lower Bollinger band (``bb_days`` SMA - ``bb_sd`` standard deviations).

Long only, daily bars:

- **Regime:** SPY closes above its 200-day SMA.
- **Entry:** close > SMA(200), at least ``min_votes`` votes, price and liquidity filters.
  More votes rank first, then the lower RSI(2).
- **Stop:** entry - ``stop_atr_mult`` x ATR(14), fixed.
- **Exits (on the close, executed next open):** close above the SMA(5) or above the
  previous day's high ("bounce_exit"), or after ``time_stop_days`` ("time_stop").
- **Signature:** SPY volatility | number of votes | distance above the SMA(200).

Parameters are hypotheses until backtested (memory/decisions.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from bot.config import ConfigError
from bot.indicators import rsi, sma
from bot.strategies.base import ExitAction, PositionState, Strategy, bucket_series
from bot.strategies.common import (
    add_common,
    atr_stop,
    base_filters,
    down_streak,
    finite_bar,
    internal_bar_strength,
    join_signature,
    ohlcv_frame,
    spy_context,
)

_DISTANCE = ("near", "mid", "far")


@dataclass(frozen=True)
class ConfluenceConfig:
    sma_trend: int = 200
    sma_exit: int = 5
    rsi_period: int = 2
    rsi_below: float = 10.0
    ibs_below: float = 0.25
    streak_days: int = 3
    bb_days: int = 20
    bb_sd: float = 2.0
    min_votes: int = 2
    atr_period: int = 14
    stop_atr_mult: float = 3.0
    time_stop_days: int = 7
    min_price: float = 10.0
    min_avg_dollar_volume: float = 50_000_000
    avg_volume_days: int = 20
    limit_offset_pct: float = 0.002
    spy_vol_edges: tuple[float, float] = (0.01, 0.02)  # SPY ATR / close
    trend_distance_edges: tuple[float, float] = (0.05, 0.15)  # close / SMA200 - 1

    def __post_init__(self) -> None:
        if not 2 <= self.sma_exit < self.sma_trend:
            raise ConfigError("sma_exit must be >= 2 and shorter than sma_trend")
        if not 1 <= self.min_votes <= 4:
            raise ConfigError("min_votes must be between 1 and 4 (there are four votes)")
        if self.stop_atr_mult <= 0:
            raise ConfigError("stop_atr_mult must be > 0: every position needs a stop")
        if self.time_stop_days < 1 or self.streak_days < 1 or self.bb_days < 2:
            raise ConfigError("time_stop_days, streak_days >= 1 and bb_days >= 2")
        if not 0 < self.rsi_below < 100 or not 0 < self.ibs_below < 1 or self.bb_sd <= 0:
            raise ConfigError("rsi_below in (0, 100), ibs_below in (0, 1), bb_sd > 0")
        for name in ("spy_vol_edges", "trend_distance_edges"):
            edges = getattr(self, name)
            if len(edges) != 2 or not edges[0] < edges[1]:
                raise ConfigError(f"{name} must be two increasing numbers")


class ConfluenceReversion(Strategy):
    name: ClassVar[str] = "confluence_reversion"
    description: ClassVar[str] = (
        "Buy an uptrend dip when 2+ of RSI(2), IBS, a losing streak and the lower "
        "Bollinger band agree; sell on the bounce or after 7 days."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "sma_trend",
        "rsi_below",
        "ibs_below",
        "bb_sd",
        "stop_atr_mult",
        "time_stop_days",
    )

    config: ConfluenceConfig

    @classmethod
    def default_config(cls) -> ConfluenceConfig:
        return ConfluenceConfig()

    def features(self, bars: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        f = add_common(ohlcv_frame(bars), cfg.sma_trend, cfg.atr_period, cfg.avg_volume_days)
        close = f["close"]
        f["sma_exit"] = sma(close, cfg.sma_exit)
        f["rsi"] = rsi(close, cfg.rsi_period)
        f["ibs"] = internal_bar_strength(f)
        f["down_streak"] = down_streak(close)
        mid = sma(close, cfg.bb_days)
        f["bb_lower"] = mid - cfg.bb_sd * close.rolling(cfg.bb_days).std(ddof=0)
        f["prev_high"] = f["high"].shift(1)
        f["votes"] = (
            (f["rsi"] < cfg.rsi_below).astype(int)
            + (f["ibs"] < cfg.ibs_below).astype(int)
            + (f["down_streak"] >= cfg.streak_days).astype(int)
            + (close < f["bb_lower"]).astype(int)
        )
        return f

    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        cfg = self.config
        regime_ok, spy_vol = spy_context(features[benchmark], cfg.spy_vol_edges)
        tables = {}
        for symbol, f in features.items():
            needed = ["sma_trend", "sma_exit", "rsi", "bb_lower", "atr", "avg_dollar_volume"]
            t = base_filters(f, regime_ok, needed, cfg.min_price, cfg.min_avg_dollar_volume)
            t["uptrend"] = f["close"] > f["sma_trend"]
            t["oversold"] = f["votes"] >= cfg.min_votes
            t["setup"] = t.all(axis=1)
            t["signature"] = join_signature(
                t,
                [
                    spy_vol.reindex(f.index),
                    "v" + f["votes"].astype(str),
                    bucket_series(
                        f["close"] / f["sma_trend"] - 1, cfg.trend_distance_edges, _DISTANCE
                    ),
                ],
            )
            # Votes dominate (RSI is within 0-100), then the deeper RSI(2) dip.
            t["score"] = f["votes"] * 1000.0 - f["rsi"]
            tables[symbol] = t
        return tables

    def initial_stop(self, entry: float, atr: float) -> float:
        return atr_stop(entry, atr, self.config.stop_atr_mult)

    def on_close(
        self, pos: PositionState, bar: Mapping[str, float]
    ) -> tuple[list[ExitAction], float]:
        close, sma_exit, prev_high = finite_bar(bar, "close", "sma_exit", "prev_high")
        if close > sma_exit or close > prev_high:
            return [ExitAction("bounce_exit", pos.shares)], pos.stop
        if pos.days_held >= self.config.time_stop_days:
            return [ExitAction("time_stop", pos.shares)], pos.stop
        return [], pos.stop
