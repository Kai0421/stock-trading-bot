# Author: Kai0421
"""Trend Pullback (docs/03-strategy.md) behind the ``Strategy`` interface.

The rules themselves live in ``bot.strategy`` as pure functions; this class only adapts
them. Signatures stay unprefixed so the existing evidence file keeps working.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

import pandas as pd

from bot import strategy as rules
from bot.config import StrategyConfig
from bot.strategies.base import ExitAction, PositionState, Strategy


class TrendPullback(Strategy):
    name: ClassVar[str] = "trend_pullback"
    description: ClassVar[str] = (
        "Uptrend (SMA50 > SMA200) pullback (RSI(2) dip or 20 EMA touch) with a close above "
        "the previous high; ATR stop, breakeven/trailing stop, configurable exit set."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "sma_fast",
        "sma_slow",
        "ema_pullback",
        "rsi_entry_below",
        "stop_atr_mult",
        "trail_atr_mult",
        "partial_profit_r",
        "breakeven_at_r",
        "time_stop_days",
    )
    signature_prefix: ClassVar[str | None] = ""

    config: StrategyConfig

    @classmethod
    def default_config(cls) -> StrategyConfig:
        return StrategyConfig()

    def features(self, bars: pd.DataFrame) -> pd.DataFrame:
        return rules.compute_features(bars, self.config)

    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        regime = rules.regime_table(features[benchmark], self.config)
        tables = {}
        for s, f in features.items():
            t = rules.entry_table(f, regime, self.config)
            t["score"] = f["momentum"]  # strongest 126-day momentum first (docs/03)
            tables[s] = t
        return tables

    def limit_price(self, last_price: float) -> float:
        return rules.limit_price(last_price, self.config)

    def initial_stop(self, entry: float, atr: float) -> float:
        return rules.initial_stop(entry, atr, self.config)

    def on_close(
        self, pos: PositionState, bar: Mapping[str, float]
    ) -> tuple[list[ExitAction], float]:
        cfg = self.config
        actions = rules.exit_actions(pos, bar["close"], bar["sma_fast"], cfg, bar["sma_exit"])
        stop = rules.update_stop(pos, bar["close"], bar["atr"], bar["ema"], cfg)
        return actions, stop
