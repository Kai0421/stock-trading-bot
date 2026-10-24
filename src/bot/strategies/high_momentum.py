# Author: Kai0421
"""52-week-high momentum: buy strong stocks breaking out near their yearly high.

Past 3-to-12-month winners keep outperforming (Jegadeesh and Titman 1993), and closeness
to the 52-week high explains much of that without the later reversal (George and Hwang
2004). Momentum crashes in sharp rebounds after bear markets (Daniel and Moskowitz 2016),
so it only trades in a market uptrend (docs/14-research-strategies.md). Long only:

- **Regime:** SPY closes above its 200-day SMA.
- **Entry:** close within ``near_high_pct`` of the 252-day high, 126-day return above
  ``min_momentum``, close above the previous ``breakout_days``-day high, price and
  liquidity filters. Strongest 126-day return ranks first.
- **Stop:** entry - ``stop_atr_mult`` x ATR(14), then trailed on each close to
  close - ``trail_atr_mult`` x ATR (it only moves up).
- **Exits (on the close, executed next open):** close below the SMA(50) ("trend_break"),
  or after ``time_stop_days`` ("time_stop").
- **Signature:** SPY volatility | momentum strength | distance from the 52-week high.

Wins less often than the reversion strategies and relies on larger winners, so it is
aimed at the expectancy gate. Parameters are hypotheses until backtested.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from bot.config import ConfigError
from bot.indicators import sma
from bot.strategies.base import ExitAction, PositionState, Strategy, bucket_series, floor_cents
from bot.strategies.common import (
    add_common,
    atr_stop,
    base_filters,
    finite_bar,
    join_signature,
    ohlcv_frame,
    spy_context,
)

_STRENGTH = ("moderate", "strong", "extreme")
_NEARNESS = ("at_high", "close", "near")


@dataclass(frozen=True)
class MomentumConfig:
    sma_trend: int = 200
    sma_exit: int = 50
    high_days: int = 252
    momentum_days: int = 126
    min_momentum: float = 0.10
    near_high_pct: float = 0.05
    breakout_days: int = 20
    atr_period: int = 14
    stop_atr_mult: float = 2.5
    trail_atr_mult: float = 3.0
    time_stop_days: int = 60
    min_price: float = 10.0
    min_avg_dollar_volume: float = 50_000_000
    avg_volume_days: int = 20
    limit_offset_pct: float = 0.002
    spy_vol_edges: tuple[float, float] = (0.01, 0.02)  # SPY ATR / close
    momentum_edges: tuple[float, float] = (0.2, 0.4)  # 126-day return
    nearness_edges: tuple[float, float] = (0.01, 0.03)  # 1 - close / 52-week high

    def __post_init__(self) -> None:
        if not 2 <= self.sma_exit < self.sma_trend:
            raise ConfigError("sma_exit must be >= 2 and shorter than sma_trend")
        if not 2 <= self.breakout_days < self.high_days or self.momentum_days < 2:
            raise ConfigError("need 2 <= breakout_days < high_days and momentum_days >= 2")
        if self.stop_atr_mult <= 0 or self.trail_atr_mult <= 0:
            raise ConfigError("stop_atr_mult and trail_atr_mult must be > 0")
        if not 0 < self.near_high_pct < 1 or self.time_stop_days < 1:
            raise ConfigError("near_high_pct in (0, 1) and time_stop_days >= 1")
        for name in ("spy_vol_edges", "momentum_edges", "nearness_edges"):
            edges = getattr(self, name)
            if len(edges) != 2 or not edges[0] < edges[1]:
                raise ConfigError(f"{name} must be two increasing numbers")


class HighMomentum(Strategy):
    name: ClassVar[str] = "high_momentum"
    description: ClassVar[str] = (
        "Buy 6-month winners breaking out within 5% of their 52-week high in a market "
        "uptrend; trail a 3 ATR stop, sell below the 50-day SMA or after 60 days."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "sma_exit",
        "min_momentum",
        "near_high_pct",
        "breakout_days",
        "stop_atr_mult",
        "trail_atr_mult",
        "time_stop_days",
    )

    config: MomentumConfig

    @classmethod
    def default_config(cls) -> MomentumConfig:
        return MomentumConfig()

    def features(self, bars: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        f = add_common(ohlcv_frame(bars), cfg.sma_trend, cfg.atr_period, cfg.avg_volume_days)
        close = f["close"]
        f["sma_exit"] = sma(close, cfg.sma_exit)
        f["high_52w"] = f["high"].rolling(cfg.high_days).max()
        f["momentum"] = close / close.shift(cfg.momentum_days) - 1
        f["breakout_level"] = f["high"].rolling(cfg.breakout_days).max().shift(1)
        return f

    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        cfg = self.config
        regime_ok, spy_vol = spy_context(features[benchmark], cfg.spy_vol_edges)
        tables = {}
        for symbol, f in features.items():
            needed = [
                "sma_trend",
                "sma_exit",
                "high_52w",
                "momentum",
                "breakout_level",
                "atr",
                "avg_dollar_volume",
            ]
            t = base_filters(f, regime_ok, needed, cfg.min_price, cfg.min_avg_dollar_volume)
            close = f["close"]
            t["uptrend"] = close > f["sma_trend"]
            t["near_high"] = close >= f["high_52w"] * (1 - cfg.near_high_pct)
            t["strong"] = f["momentum"] > cfg.min_momentum
            t["breakout"] = close > f["breakout_level"]
            t["setup"] = t.all(axis=1)
            t["signature"] = join_signature(
                t,
                [
                    spy_vol.reindex(f.index),
                    bucket_series(f["momentum"], cfg.momentum_edges, _STRENGTH),
                    bucket_series(1 - close / f["high_52w"], cfg.nearness_edges, _NEARNESS),
                ],
            )
            t["score"] = f["momentum"]
            tables[symbol] = t
        return tables

    def initial_stop(self, entry: float, atr: float) -> float:
        return atr_stop(entry, atr, self.config.stop_atr_mult)

    def on_close(
        self, pos: PositionState, bar: Mapping[str, float]
    ) -> tuple[list[ExitAction], float]:
        close, atr, sma_exit = finite_bar(bar, "close", "atr", "sma_exit")
        trail = floor_cents(close - self.config.trail_atr_mult * atr)
        stop = max(pos.stop, trail) if trail < close else pos.stop
        if close < sma_exit:
            return [ExitAction("trend_break", pos.shares)], stop
        if pos.days_held >= self.config.time_stop_days:
            return [ExitAction("time_stop", pos.shares)], stop
        return [], stop
