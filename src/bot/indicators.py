# Author: Kai0421
"""Technical indicators on daily bars: SMA, EMA, RSI and ATR.

Conventions (match the common Wilder / TA-Lib definitions):
- The first ``period - 1`` values are NaN (not enough history).
- EMA is seeded with the SMA of the first ``period`` values, then
  ``ema_t = alpha * x_t + (1 - alpha) * ema_{t-1}`` with ``alpha = 2 / (period + 1)``.
- RSI and ATR use Wilder smoothing (``alpha = 1 / period``), seeded with a simple average.
- RSI is 100 when there are only gains, 0 when only losses and 50 when prices are flat.
- True range of the first bar is ``high - low`` (no previous close).

Inputs must be clean: NaNs raise ``ValueError`` instead of silently producing signals.
Inputs are never modified; outputs keep the input index.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _check_period(period: int) -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError(f"period must be a positive int, got {period!r}")


def _check_series(series: pd.Series, name: str = "series") -> np.ndarray:
    values = series.to_numpy(dtype="float64", copy=True)
    if np.isnan(values).any():
        raise ValueError(f"{name} contains NaN; clean the bars before computing indicators")
    return values


def _wilder(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing seeded with the mean of the first ``period`` values."""
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def sma(close: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    _check_period(period)
    _check_series(close, "close")
    out = close.astype("float64").rolling(window=period, min_periods=period).mean()
    return out.rename(f"sma_{period}")


def ema(close: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, SMA-seeded."""
    _check_period(period)
    values = _check_series(close, "close")
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        alpha = 2.0 / (period + 1)
        out[period - 1] = values[:period].mean()
        for i in range(period, len(values)):
            out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=close.index, name=f"ema_{period}")


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's Relative Strength Index (0-100)."""
    _check_period(period)
    values = _check_series(close, "close")
    out = np.full(len(values), np.nan)
    if len(values) > period:
        change = np.diff(values)
        avg_gain = _wilder(np.clip(change, 0, None), period)
        avg_loss = _wilder(np.clip(-change, 0, None), period)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        r = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, r)
        r = np.where((avg_loss == 0) & (avg_gain == 0), 50.0, r)
        out[1:] = r
    return pd.Series(out, index=close.index, name=f"rsi_{period}")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range: max(high - low, |high - prev_close|, |low - prev_close|)."""
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high, low and close must have the same length")
    if not (high.index.equals(low.index) and high.index.equals(close.index)):
        raise ValueError("high, low and close must share the same index")
    h = _check_series(high, "high")
    lo = _check_series(low, "low")
    c = _check_series(close, "close")
    if (h < lo).any():
        raise ValueError("high is below low in at least one bar")
    tr = h - lo
    if len(c) > 1:
        prev = c[:-1]
        tr[1:] = np.maximum.reduce([tr[1:], np.abs(h[1:] - prev), np.abs(lo[1:] - prev)])
    return pd.Series(tr, index=close.index, name="true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder's Average True Range."""
    _check_period(period)
    tr = true_range(high, low, close).to_numpy()
    return pd.Series(_wilder(tr, period), index=close.index, name=f"atr_{period}")
