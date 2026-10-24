# Author: Kai0421
"""Shared building blocks for the price-only strategies (docs/14-research-strategies.md)."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from bot.indicators import atr, sma
from bot.strategies.base import OHLCV, bucket_series, floor_cents, require_finite

VOL_LABELS = ("low", "mid", "high")


def ohlcv_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """A float copy of the OHLCV columns, after checking the bars are usable."""
    missing = [c for c in OHLCV if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns: {missing}")
    if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
        raise ValueError("bars index must be sorted ascending with no duplicates")
    return bars.loc[:, list(OHLCV)].astype("float64")


def internal_bar_strength(f: pd.DataFrame) -> pd.Series:
    """Where the close sits in the day's range: 0 at the low, 1 at the high, 0.5 when
    the bar has no range."""
    span = f["high"] - f["low"]
    ibs = (f["close"] - f["low"]) / span.where(span > 0)
    return ibs.fillna(0.5).clip(0.0, 1.0).rename("ibs")


def down_streak(close: pd.Series) -> pd.Series:
    """Consecutive closes below the previous close, ending at each bar."""
    down = (close < close.shift(1)).astype(int)
    runs = (down == 0).cumsum()
    return down.groupby(runs).cumsum().rename("down_streak")


def add_common(
    f: pd.DataFrame, sma_trend: int, atr_period: int, avg_volume_days: int
) -> pd.DataFrame:
    """Trend average, ATR and average dollar volume, the columns every strategy here uses."""
    close = f["close"]
    f["sma_trend"] = sma(close, sma_trend)
    f["atr"] = atr(f["high"], f["low"], close, atr_period)
    f["avg_dollar_volume"] = (close * f["volume"]).rolling(avg_volume_days).mean()
    return f


def spy_context(spy: pd.DataFrame, vol_edges: tuple[float, float]) -> tuple[pd.Series, pd.Series]:
    """Market regime (SPY above its trend average, known history only) and the SPY
    volatility bucket used in signatures."""
    known = spy["sma_trend"].notna() & spy["atr"].notna()
    regime_ok = known & (spy["close"] > spy["sma_trend"])
    vol = bucket_series(spy["atr"] / spy["close"], vol_edges, VOL_LABELS)
    return regime_ok, vol


def base_filters(
    f: pd.DataFrame,
    regime_ok: pd.Series,
    needed: list[str],
    min_price: float,
    min_avg_dollar_volume: float,
) -> pd.DataFrame:
    """Filter columns shared by every entry table: enough history, market regime,
    price and liquidity."""
    t = pd.DataFrame(index=f.index)
    t["history_ok"] = f[needed].notna().all(axis=1)
    t["regime_ok"] = regime_ok.reindex(f.index).eq(True)
    t["price_ok"] = f["close"] > min_price
    t["volume_ok"] = f["avg_dollar_volume"] > min_avg_dollar_volume
    return t


def join_signature(t: pd.DataFrame, parts: list[pd.Series]) -> pd.Series:
    sig = parts[0].astype(str)
    for p in parts[1:]:
        sig = sig + "|" + p.astype(str)
    return sig.where(t["setup"], None)


def atr_stop(entry: float, atr_value: float, mult: float) -> float:
    require_finite(atr=atr_value)
    if not atr_value > 0:
        raise ValueError(f"ATR must be a positive number (got {atr_value})")
    return floor_cents(entry - mult * atr_value)


def finite_bar(bar: Mapping[str, float], *keys: str) -> list[float]:
    values = [bar[k] for k in keys]
    require_finite(**dict(zip(keys, values, strict=True)))
    return values
