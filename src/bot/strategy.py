# Author: Kai0421
"""Trend Pullback strategy on daily bars, long only (docs/03-strategy.md).

The rules as pure functions; ``bot.strategies.trend_pullback`` plugs them into the
``Strategy`` interface the backtester and engine use.

Pure functions on OHLCV DataFrames; nothing here touches the broker. Bars passed in
must end at the evaluation date (the backtester slices them; no look-ahead).

- ``market_regime``: SPY close > SPY 200 SMA, plus the SPY part of the setup signature.
- ``evaluate_entry``: uptrend, pullback (RSI(2) < 10 or low touched the 20 EMA within
  the lookback), trigger (close > previous high), universe filters and earnings blackout.
  A passing setup becomes a ``Candidate`` with its ``SetupSignature``; the probability
  gate (bot.risk) decides whether it may trade.
- ``update_stop`` / ``exit_actions``: breakeven at +1R, then trail; partial at +2R,
  trend break, time stop. Stops only move up.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from bot.config import StrategyConfig
from bot.indicators import atr, ema, rsi, sma
from bot.strategies.base import OHLCV, ExitAction, PositionState
from bot.strategies.base import bucket_series as _bucket_series
from bot.strategies.base import floor_cents as _floor_cents
from bot.strategies.base import require_finite as _require_finite

_FEATURES = ("sma_fast", "sma_slow", "ema", "rsi", "atr", "momentum", "avg_dollar_volume")


def bucket(value: float, edges: tuple[float, float], labels: tuple[str, str, str]) -> str:
    """``labels[0]`` below ``edges[0]``, ``labels[1]`` below ``edges[1]``, else ``labels[2]``."""
    if math.isnan(value):
        raise ValueError("cannot bucket NaN")
    if value < edges[0]:
        return labels[0]
    if value < edges[1]:
        return labels[1]
    return labels[2]


def compute_features(bars: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """OHLCV plus the indicator columns the strategy uses. The input is not modified."""
    missing = [c for c in OHLCV if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns: {missing}")
    if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
        raise ValueError("bars index must be sorted ascending with no duplicates")
    f = bars.loc[:, list(OHLCV)].astype("float64")
    close = f["close"]
    f["sma_fast"] = sma(close, cfg.sma_fast)
    f["sma_slow"] = sma(close, cfg.sma_slow)
    f["ema"] = ema(close, cfg.ema_pullback)
    f["sma_exit"] = sma(close, cfg.mr_exit_sma)
    f["rsi"] = rsi(close, cfg.rsi_period)
    f["atr"] = atr(f["high"], f["low"], close, cfg.atr_period)
    f["momentum"] = close / close.shift(cfg.momentum_days) - 1
    dollar_volume = close * f["volume"]
    f["avg_dollar_volume"] = dollar_volume.rolling(cfg.avg_volume_days).mean()
    return f


# --------------------------------------------------------- regime


@dataclass(frozen=True)
class Regime:
    as_of: pd.Timestamp | None
    ok: bool
    trend: str  # "up" / "down" / "unknown"
    vol_bucket: str  # "low" / "mid" / "high" / "unknown"


_VOL_LABELS = ("low", "mid", "high")
_TREND_LABELS = ("weak", "moderate", "strong")
_DEPTH_LABELS = ("deep", "moderate", "shallow")
_DISTANCE_LABELS = ("near", "mid", "far")


def regime_table(spy_features: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Per-date regime from SPY features: ``ok``, ``trend`` and ``vol_bucket``."""
    f = spy_features
    known = f["sma_slow"].notna() & f["atr"].notna()
    up = known & (f["close"] > f["sma_slow"])
    trend = pd.Series(np.where(up, "up", "down"), index=f.index, dtype=object)
    trend[~known] = "unknown"
    vol = _bucket_series(f["atr"] / f["close"], cfg.spy_vol_edges, _VOL_LABELS)
    vol[~known] = "unknown"
    return pd.DataFrame({"ok": up, "trend": trend, "vol_bucket": vol})


def market_regime(spy_bars: pd.DataFrame, cfg: StrategyConfig) -> Regime:
    """New entries only when SPY closes above its 200-day SMA. Unknown means no entries."""
    if spy_bars.empty:
        return Regime(None, False, "unknown", "unknown")
    last = regime_table(compute_features(spy_bars, cfg), cfg).iloc[-1]
    return Regime(spy_bars.index[-1], bool(last["ok"]), last["trend"], last["vol_bucket"])


# --------------------------------------------------------- entry


@dataclass(frozen=True)
class SetupSignature:
    spy_trend: str
    spy_vol: str
    trend_strength: str
    pullback_depth: str
    ema_distance: str

    def key(self) -> str:
        return "|".join(
            (
                self.spy_trend,
                self.spy_vol,
                self.trend_strength,
                self.pullback_depth,
                self.ema_distance,
            )
        )


@dataclass(frozen=True)
class Candidate:
    symbol: str
    as_of: pd.Timestamp
    close: float
    prev_high: float
    atr: float
    ema: float
    sma_fast: float
    momentum: float
    avg_dollar_volume: float
    signature: SetupSignature


@dataclass(frozen=True)
class EntryEvaluation:
    symbol: str
    as_of: pd.Timestamp | None
    candidate: Candidate | None
    reasons: tuple[str, ...]  # why there is no candidate; empty when there is one


def entry_table(features: pd.DataFrame, regime: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Entry rules for every bar in one pass (earnings excluded; callers add it).

    Every column at row ``i`` uses only rows ``<= i``, so the scan equals running
    ``evaluate_entry`` on each prefix. Regime dates missing from ``regime`` fail closed.
    """
    f = features
    lookback = cfg.pullback_lookback_days
    reg = regime.reindex(f.index)
    position = pd.Series(np.arange(len(f)), index=f.index)
    window_ok = (f["rsi"].notna() & f["ema"].notna()).astype(float).rolling(lookback).min() == 1
    history_ok = f[list(_FEATURES)].notna().all(axis=1) & window_ok & (position >= lookback)

    close = f["close"]
    prev_high = f["high"].shift(1)
    rsi_min = f["rsi"].rolling(lookback).min()
    touched = (f["low"] <= f["ema"]).astype(float).rolling(lookback).max() == 1
    t = pd.DataFrame(index=f.index)
    t["history_ok"] = history_ok
    t["regime_ok"] = reg["ok"].eq(True)
    t["price_ok"] = close > cfg.min_price
    t["volume_ok"] = f["avg_dollar_volume"] > cfg.min_avg_dollar_volume
    t["uptrend"] = (close > f["sma_slow"]) & (f["sma_fast"] > f["sma_slow"])
    t["pullback"] = (rsi_min < cfg.rsi_entry_below) | touched
    t["trigger"] = close > prev_high
    t["setup"] = t.all(axis=1)
    t["rsi_min"] = rsi_min
    t["prev_high"] = prev_high
    t["spy_trend"] = reg["trend"]
    t["spy_vol"] = reg["vol_bucket"]
    t["trend_strength"] = _bucket_series(
        f["sma_fast"] / f["sma_slow"] - 1, cfg.trend_strength_edges, _TREND_LABELS
    )
    t["pullback_depth"] = _bucket_series(rsi_min, cfg.rsi_depth_edges, _DEPTH_LABELS)
    t["ema_distance"] = _bucket_series(
        (close - f["ema"]) / f["atr"], cfg.ema_distance_edges, _DISTANCE_LABELS
    )
    parts = ["spy_trend", "spy_vol", "trend_strength", "pullback_depth", "ema_distance"]
    sig = t[parts[0]].astype(str)
    for p in parts[1:]:
        sig = sig + "|" + t[p].astype(str)
    t["signature"] = sig.where(t["setup"], None)
    return t


def evaluate_entry(
    symbol: str,
    bars: pd.DataFrame,
    regime: Regime,
    cfg: StrategyConfig,
    earnings_in_blackout: bool | None,
) -> EntryEvaluation:
    """Check the entry rules on the last bar. ``earnings_in_blackout=None`` (unknown) rejects."""
    if len(bars) < 2:
        as_of = bars.index[-1] if len(bars) else None
        return EntryEvaluation(symbol, as_of, None, ("insufficient history",))
    f = compute_features(bars, cfg)
    as_of = f.index[-1]
    reg = pd.DataFrame(
        {"ok": [regime.ok], "trend": [regime.trend], "vol_bucket": [regime.vol_bucket]},
        index=[regime.as_of],
    )
    row = entry_table(f, reg, cfg).iloc[-1]
    if not row["history_ok"]:
        return EntryEvaluation(symbol, as_of, None, ("insufficient history",))

    last = f.iloc[-1]
    close = float(last["close"])
    reasons: list[str] = []
    if regime.as_of != as_of:
        reasons.append(f"regime date mismatch ({regime.as_of} vs {as_of})")
    elif not row["regime_ok"]:
        reasons.append("market regime not ok (SPY not above its 200 SMA)")
    if not row["price_ok"]:
        reasons.append(f"price {close:.2f} not above {cfg.min_price:.2f}")
    if not row["volume_ok"]:
        reasons.append(f"avg dollar volume {last['avg_dollar_volume']:,.0f} too low")
    if not row["uptrend"]:
        reasons.append("no uptrend (need close > SMA200 and SMA50 > SMA200)")
    if not row["pullback"]:
        reasons.append("no pullback (RSI(2) dip or 20 EMA touch) in lookback")
    if not row["trigger"]:
        reasons.append("no trigger (close not above previous high)")
    if earnings_in_blackout is None:
        reasons.append("earnings date unknown")
    elif earnings_in_blackout:
        reasons.append(f"earnings within {cfg.earnings_blackout_days} trading days")
    if reasons:
        return EntryEvaluation(symbol, as_of, None, tuple(reasons))

    signature = SetupSignature(
        spy_trend=row["spy_trend"],
        spy_vol=row["spy_vol"],
        trend_strength=row["trend_strength"],
        pullback_depth=row["pullback_depth"],
        ema_distance=row["ema_distance"],
    )
    candidate = Candidate(
        symbol=symbol,
        as_of=as_of,
        close=close,
        prev_high=float(row["prev_high"]),
        atr=float(last["atr"]),
        ema=float(last["ema"]),
        sma_fast=float(last["sma_fast"]),
        momentum=float(last["momentum"]),
        avg_dollar_volume=float(last["avg_dollar_volume"]),
        signature=signature,
    )
    return EntryEvaluation(symbol, as_of, candidate, ())


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    win_prob: float  # from the probability gate


def rank_candidates(scored: Sequence[ScoredCandidate]) -> list[ScoredCandidate]:
    """Highest success probability first, then strongest 126-day momentum."""
    return sorted(scored, key=lambda s: (-s.win_prob, -s.candidate.momentum))


# --------------------------------------------------------- prices and stops


def limit_price(last_price: float, cfg: StrategyConfig) -> float:
    """Entry limit = last price + offset, rounded to the cent."""
    if not last_price > 0:
        raise ValueError(f"last price must be positive (got {last_price})")
    return round(last_price * (1 + cfg.limit_offset_pct), 2)


def initial_stop(entry: float, atr_value: float, cfg: StrategyConfig) -> float:
    """Entry - stop_atr_mult x ATR, floored to the cent. This distance defines R."""
    if not (math.isfinite(atr_value) and atr_value > 0):
        raise ValueError(f"ATR must be a positive number (got {atr_value})")
    stop = _floor_cents(entry - cfg.stop_atr_mult * atr_value)
    if not 0 < stop < entry:
        raise ValueError(f"stop {stop} is not between 0 and entry {entry}")
    return stop


def update_stop(
    pos: PositionState, close: float, atr: float, ema: float, cfg: StrategyConfig
) -> float:
    """New stop after today's close. Never lower than the current stop, always below close."""
    _require_finite(close=close, atr=atr, ema=ema)
    if pos.r_multiple(close) < cfg.breakeven_at_r and pos.stop < pos.entry_price:
        return pos.stop
    levels = (
        pos.entry_price,
        close - cfg.trail_atr_mult * atr,
        ema - cfg.trail_ema_buffer_atr * atr,
    )
    usable = [_floor_cents(lv) for lv in levels if lv < close]
    return max([pos.stop, *usable])


def exit_actions(
    pos: PositionState,
    close: float,
    sma_fast: float,
    cfg: StrategyConfig,
    sma_exit: float | None = None,
) -> list[ExitAction]:
    """Exits decided on the daily close, executed next session. Full exits win over partials.

    ``sma_exit`` (the SMA(5) close) is required by the ``mean_reversion`` exit set.
    """
    _require_finite(close=close, sma_fast=sma_fast)
    if cfg.exit_set.startswith("mean_reversion"):
        if sma_exit is None:
            raise ValueError("the mean_reversion exit set needs sma_exit")
        _require_finite(sma_exit=sma_exit)
    r_now = pos.r_multiple(close)
    if close < sma_fast:
        return [ExitAction("trend_break", pos.shares)]
    if pos.days_held >= cfg.time_stop_days and r_now < cfg.time_stop_min_r:
        return [ExitAction("time_stop", pos.shares)]
    if cfg.exit_set.startswith("mean_reversion"):
        target = r_now > 0 and (close > (sma_exit or 0) or r_now >= cfg.mr_target_r)
        if target and cfg.exit_set == "mean_reversion_full":
            return [ExitAction("profit_target", pos.shares)]
    else:
        target = r_now >= cfg.partial_profit_r
    if not pos.partial_taken and target:
        qty = math.floor(pos.shares * cfg.partial_profit_fraction)
        if 0 < qty < pos.shares:
            return [ExitAction("partial_profit", qty)]
    return []
