# Author: Kai0421
"""Analyst desk as a strategy: buy when the desk upgrades a stock to Overweight or Buy.

Uses the two desk analysts that only need daily bars (technical and market,
bot.desk.analysts), so it can be backtested over any period. Fundamentals, insiders and
news have no history in the backtester; they stay in the research report
(``python -m bot.desk``). Long only:

- **Rating:** desk weights over technical (30) and market (15), Overweight at a score of
  ``entry_score`` or more. Risk caps as in the desk: SPY below its 200-day SMA or ATR above
  4% of price caps the rating at Hold (no entry).
- **Entry:** the rating turns Overweight or Buy today after not being so the session
  before (an upgrade), with price and liquidity filters. Highest desk score ranks first.
- **Stop:** entry - ``stop_atr_mult`` x ATR(14), fixed.
- **Exits (on the close, executed next open):** technical score below ``exit_score``
  ("downgrade"); after ``time_stop_days`` ("time_stop").
- **Signature:** rating | SPY volatility.

Parameters are hypotheses until backtested (memory/decisions.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd

from bot.config import ConfigError
from bot.desk.analysts import market_frame, technical_frame
from bot.desk.judge import WEIGHTS
from bot.indicators import atr
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
_TECH = ("tech_score", "high_volatility")


@dataclass(frozen=True)
class DeskConfig:
    entry_score: float = 0.2  # Overweight in the desk's rating scale
    buy_score: float = 0.5  # Buy
    exit_score: float = -0.2  # technical analyst turns bearish
    atr_period: int = 14
    stop_atr_mult: float = 2.5
    time_stop_days: int = 20
    min_price: float = 10.0
    min_avg_dollar_volume: float = 50_000_000
    avg_volume_days: int = 20
    limit_offset_pct: float = 0.002
    spy_vol_edges: tuple[float, float] = (0.01, 0.02)  # SPY ATR / close

    def __post_init__(self) -> None:
        if not self.exit_score < self.entry_score <= self.buy_score:
            raise ConfigError("need exit_score < entry_score <= buy_score")
        if self.stop_atr_mult <= 0:
            raise ConfigError("stop_atr_mult must be > 0: every position needs a stop")
        if self.time_stop_days < 1:
            raise ConfigError("time_stop_days must be >= 1")
        if len(self.spy_vol_edges) != 2 or not self.spy_vol_edges[0] < self.spy_vol_edges[1]:
            raise ConfigError("spy_vol_edges must be two increasing numbers")


class AnalystDesk(Strategy):
    name: ClassVar[str] = "analyst_desk"
    description: ClassVar[str] = (
        "Buy when the analyst desk's technical and market analysts upgrade a stock to "
        "Overweight; sell when the technical view turns bearish or after 20 days."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "entry_score",
        "exit_score",
        "stop_atr_mult",
        "time_stop_days",
    )

    config: DeskConfig

    @classmethod
    def default_config(cls) -> DeskConfig:
        return DeskConfig()

    def features(self, bars: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        missing = [c for c in OHLCV if c not in bars.columns]
        if missing:
            raise ValueError(f"bars are missing columns: {missing}")
        if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
            raise ValueError("bars index must be sorted ascending with no duplicates")
        f = bars.loc[:, list(OHLCV)].astype("float64")
        tech = technical_frame(f)
        f["tech_score"] = tech["tech_score"]
        f["high_volatility"] = tech["high_volatility"].astype("float64")
        f["atr"] = atr(f["high"], f["low"], f["close"], cfg.atr_period)
        f["avg_dollar_volume"] = (f["close"] * f["volume"]).rolling(cfg.avg_volume_days).mean()
        return f

    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        cfg = self.config
        spy = features[benchmark]
        mkt = market_frame(spy)
        spy_vol = bucket_series(spy["atr"] / spy["close"], cfg.spy_vol_edges, _VOL)
        w_tech, w_mkt = WEIGHTS["technical"], WEIGHTS["market"]

        tables = {}
        for symbol, f in features.items():
            m = mkt.reindex(f.index)
            score = (w_tech * f["tech_score"] + w_mkt * m["mkt_score"]) / (w_tech + w_mkt)
            capped = m["market_downtrend"].eq(True) | f["high_volatility"].eq(1.0)
            t = pd.DataFrame(index=f.index)
            t["history_ok"] = score.notna() & f[["atr", "avg_dollar_volume"]].notna().all(axis=1)
            t["bullish"] = t["history_ok"] & (score >= cfg.entry_score) & ~capped
            t["upgrade"] = t["bullish"] & ~t["bullish"].shift(1, fill_value=False)
            t["price_ok"] = f["close"] > cfg.min_price
            t["volume_ok"] = f["avg_dollar_volume"] > cfg.min_avg_dollar_volume
            t["setup"] = t[["upgrade", "price_ok", "volume_ok"]].all(axis=1)
            rating = pd.Series(np.where(score >= cfg.buy_score, "Buy", "Overweight"), f.index)
            t["signature"] = (rating + "|" + spy_vol.reindex(f.index).astype(str)).where(
                t["setup"], None
            )
            t["score"] = score
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
        tech = bar["tech_score"]
        require_finite(close=bar["close"])
        if tech == tech and tech < self.config.exit_score:  # NaN never triggers
            return [ExitAction("downgrade", pos.shares)], pos.stop
        if pos.days_held >= self.config.time_stop_days:
            return [ExitAction("time_stop", pos.shares)], pos.stop
        return [], pos.stop
