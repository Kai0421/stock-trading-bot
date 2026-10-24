# Author: Kai0421
"""Kronos rank: buy the stocks the Kronos model forecasts to rise over the next week.

Opt-in experiment (``STRATEGY=kronos_rank``); nothing changes for the other strategies. The
forecasts come from a cache file written by ``python -m bot.kronos`` (bot/kronos.py); this
strategy only reads it, so it needs no torch. A bar with no cached forecast is never a setup
(fail closed). Long only, daily bars:

- **Regime:** SPY closes above its 200-day SMA (no entries otherwise).
- **Entry:** Kronos P(close in ``horizon`` days > today's close) >= ``min_p_up`` and mean
  forecast return >= ``min_fret``, with price and liquidity filters. Highest forecast return
  ranks first. Entry at the next open like every strategy.
- **Stop:** entry - ``stop_atr_mult`` x ATR(14), fixed.
- **Exit:** after ``time_stop_days`` (the forecast horizon), sell everything ("time_stop").
- **Signature:** SPY volatility | P(up) bucket | forecast-return bucket.

It still has to pass the probability gate like every strategy. Offline tests on 2025-09 to
2026-10 found no reliable edge for zero-shot Kronos-small (memory/decisions.md, 2026-10-09).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from bot.config import ConfigError
from bot.indicators import atr, sma
from bot.kronos import DEFAULT_FORECASTS, DEFAULT_MODEL, read_forecasts
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
_P_UP = ("lean", "likely", "strong")
_FRET = ("small", "mid", "big")


@dataclass(frozen=True)
class KronosConfig:
    forecast_path: str = str(DEFAULT_FORECASTS)
    model: str = DEFAULT_MODEL
    seed: int = 0
    lookback: int = 400
    horizon: int = 5
    samples: int = 30
    min_p_up: float = 0.6
    min_fret: float = 0.01
    sma_trend: int = 200
    atr_period: int = 14
    stop_atr_mult: float = 2.5
    time_stop_days: int = 5
    min_price: float = 10.0
    min_avg_dollar_volume: float = 50_000_000
    avg_volume_days: int = 20
    limit_offset_pct: float = 0.002
    spy_vol_edges: tuple[float, float] = (0.01, 0.02)  # SPY ATR / close
    p_up_edges: tuple[float, float] = (0.7, 0.85)
    fret_edges: tuple[float, float] = (0.02, 0.04)

    def __post_init__(self) -> None:
        if self.stop_atr_mult <= 0:
            raise ConfigError("stop_atr_mult must be > 0: every position needs a stop")
        if self.time_stop_days < 1 or self.horizon < 1 or self.samples < 1:
            raise ConfigError("time_stop_days, horizon and samples must be >= 1")
        if not 0.5 <= self.min_p_up <= 1:
            raise ConfigError("min_p_up must be in [0.5, 1]: only bullish forecasts may buy")
        if self.min_fret < 0:
            raise ConfigError("min_fret must be >= 0: only rising forecasts may buy")
        for name in ("spy_vol_edges", "p_up_edges", "fret_edges"):
            edges = getattr(self, name)
            if len(edges) != 2 or not edges[0] < edges[1]:
                raise ConfigError(f"{name} must be two increasing numbers")


class KronosRank(Strategy):
    name: ClassVar[str] = "kronos_rank"
    description: ClassVar[str] = (
        "Buy stocks the Kronos model forecasts to rise over 5 days (opt-in experiment)."
    )
    stability_params: ClassVar[tuple[str, ...]] = (
        "min_p_up",
        "min_fret",
        "stop_atr_mult",
        "time_stop_days",
    )

    config: KronosConfig

    @classmethod
    def default_config(cls) -> KronosConfig:
        return KronosConfig()

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
        f["atr"] = atr(f["high"], f["low"], close, cfg.atr_period)
        f["avg_dollar_volume"] = (close * f["volume"]).rolling(cfg.avg_volume_days).mean()
        return f

    def forecasts(self) -> pd.DataFrame:
        cfg = self.config
        return read_forecasts(cfg.forecast_path, cfg.model, cfg.seed)

    def entry_tables(
        self, features: Mapping[str, pd.DataFrame], benchmark: str
    ) -> dict[str, pd.DataFrame]:
        cfg = self.config
        cached = self.forecasts()
        spy = features[benchmark]
        spy_known = spy["sma_trend"].notna() & spy["atr"].notna()
        regime_ok = spy_known & (spy["close"] > spy["sma_trend"])
        spy_vol = bucket_series(spy["atr"] / spy["close"], cfg.spy_vol_edges, _VOL)

        tables = {}
        for symbol, f in features.items():
            close = f["close"]
            days = pd.DatetimeIndex(f.index).date
            if symbol in cached.index.get_level_values(0):
                fc = cached.loc[symbol].reindex(days)
            else:
                fc = pd.DataFrame(index=days, columns=["p_up", "fret"], dtype="float64")
            p_up = pd.Series(fc["p_up"].to_numpy(), index=f.index)
            fret = pd.Series(fc["fret"].to_numpy(), index=f.index)

            t = pd.DataFrame(index=f.index)
            t["history_ok"] = f[["sma_trend", "atr", "avg_dollar_volume"]].notna().all(axis=1)
            t["forecast_ok"] = p_up.notna() & fret.notna()
            t["regime_ok"] = regime_ok.reindex(f.index).eq(True)
            t["price_ok"] = close > cfg.min_price
            t["volume_ok"] = f["avg_dollar_volume"] > cfg.min_avg_dollar_volume
            t["bullish"] = (p_up >= cfg.min_p_up) & (fret >= cfg.min_fret)
            t["setup"] = t.all(axis=1)
            parts = (
                spy_vol.reindex(f.index).astype(str),
                bucket_series(p_up, cfg.p_up_edges, _P_UP).astype(str),
                bucket_series(fret, cfg.fret_edges, _FRET).astype(str),
            )
            t["signature"] = (parts[0] + "|" + parts[1] + "|" + parts[2]).where(t["setup"], None)
            t["score"] = fret
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
        if pos.days_held >= self.config.time_stop_days:
            return [ExitAction("time_stop", pos.shares)], pos.stop
        return [], pos.stop
