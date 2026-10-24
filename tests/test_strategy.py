# Author: Kai0421
"""Tests for bot.strategy (Trend Pullback, docs/03-strategy.md) on synthetic daily bars."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from bot.config import StrategyConfig
from bot.strategy import (
    ExitAction,
    PositionState,
    ScoredCandidate,
    bucket,
    compute_features,
    entry_table,
    evaluate_entry,
    exit_actions,
    initial_stop,
    limit_price,
    market_regime,
    rank_candidates,
    regime_table,
    update_stop,
)

CFG = StrategyConfig(exit_set="trend", time_stop_days=15)  # trend exit set under test


def bars(closes, spread=0.005, volume=1_000_000, start="2025-01-01"):
    c = np.asarray(closes, dtype="float64")
    idx = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame(
        {
            "open": c,
            "high": c * (1 + spread),
            "low": c * (1 - spread),
            "close": c,
            "volume": volume,
        },
        index=idx,
    )


def trend(n=260, start=100.0, step=0.003):
    return [start * (1 + step) ** i for i in range(n)]


def pullback(n=260, start=100.0):
    """Steady uptrend, three 2% down days, then a 3% up day closing above yesterday's high."""
    c = trend(n - 4, start)
    for _ in range(3):
        c.append(c[-1] * 0.98)
    c.append(c[-1] * 1.03)
    return c


SPY = bars(trend(), spread=0.003)


def regime(spy=SPY):
    return market_regime(spy, CFG)


def evaluate(df, spy=SPY, earnings=False):
    return evaluate_entry("AAPL", df, market_regime(spy, CFG), CFG, earnings_in_blackout=earnings)


# ------------------------------------------------------- features


def test_features_columns_and_input_untouched():
    df = bars(trend())
    before = df.copy()
    f = compute_features(df, CFG)
    for col in ("sma_fast", "sma_slow", "ema", "rsi", "atr", "momentum", "avg_dollar_volume"):
        assert col in f.columns
    pd.testing.assert_frame_equal(df, before)
    assert f["momentum"].iloc[-1] == pytest.approx(1.003**126 - 1)


def test_features_require_ohlcv():
    with pytest.raises(ValueError):
        compute_features(bars(trend()).drop(columns="volume"), CFG)


def test_features_require_sorted_unique_index():
    df = bars(trend())
    with pytest.raises(ValueError):
        compute_features(df.iloc[::-1], CFG)
    with pytest.raises(ValueError):
        compute_features(pd.concat([df, df.iloc[-1:]]), CFG)


# ------------------------------------------------------- regime


def test_regime_ok_in_uptrend():
    r = regime()
    assert r.ok
    assert r.trend == "up"
    assert r.vol_bucket == "low"
    assert r.as_of == SPY.index[-1]


def test_regime_blocks_below_200_sma():
    r = regime(bars(trend(step=-0.003), spread=0.003))
    assert not r.ok
    assert r.trend == "down"


def test_regime_blocks_with_short_history():
    assert not regime(bars(trend(n=150), spread=0.003)).ok


# ------------------------------------------------------- entry


def test_rsi_pullback_entry():
    ev = evaluate(bars(pullback()))
    assert ev.reasons == ()
    c = ev.candidate
    assert c is not None
    assert c.symbol == "AAPL"
    assert c.as_of == SPY.index[-1]
    assert c.signature.spy_trend == "up"
    assert c.signature.pullback_depth == "deep"
    assert c.signature.trend_strength == "strong"
    assert c.signature.key().startswith("up|low|strong|deep|")


def test_ema_touch_entry_without_low_rsi():
    df = bars(trend(step=0.01))  # rising 1%/day: trigger holds, RSI(2) stays high
    assert evaluate(df).candidate is None
    ema = compute_features(df, CFG)["ema"]
    df.iloc[-2, df.columns.get_loc("low")] = ema.iloc[-2] * 0.999
    ev = evaluate(df)
    assert ev.candidate is not None, ev.reasons


def test_no_pullback_rejected():
    ev = evaluate(bars(trend(step=0.01)))
    assert ev.candidate is None
    assert any("pullback" in r for r in ev.reasons)


def test_trigger_required():
    c = pullback()
    c[-1] = c[-2] * 0.99
    ev = evaluate(bars(c))
    assert ev.candidate is None
    assert any("trigger" in r for r in ev.reasons)


def test_downtrend_rejected():
    ev = evaluate(bars(trend(step=-0.003)))
    assert ev.candidate is None
    assert any("uptrend" in r for r in ev.reasons)


def test_bad_regime_rejected():
    ev = evaluate(bars(pullback()), spy=bars(trend(step=-0.003), spread=0.003))
    assert ev.candidate is None
    assert any("regime" in r for r in ev.reasons)


def test_regime_date_mismatch_rejected():
    ev = evaluate(bars(pullback()), spy=SPY.iloc[:-1])
    assert ev.candidate is None
    assert any("date" in r for r in ev.reasons)


@pytest.mark.parametrize(("earnings", "word"), [(True, "earnings"), (None, "unknown")])
def test_earnings_blackout_or_unknown_rejected(earnings, word):
    ev = evaluate(bars(pullback()), earnings=earnings)
    assert ev.candidate is None
    assert any(word in r for r in ev.reasons)


def test_low_price_rejected():
    ev = evaluate(bars(pullback(start=3.0), volume=100_000_000))
    assert ev.candidate is None
    assert any("price" in r for r in ev.reasons)


def test_low_dollar_volume_rejected():
    ev = evaluate(bars(pullback(), volume=100_000))
    assert ev.candidate is None
    assert any("dollar volume" in r for r in ev.reasons)


def test_short_history_rejected():
    ev = evaluate(bars(pullback(n=150)), spy=bars(trend(n=150), spread=0.003))
    assert ev.candidate is None
    assert any("history" in r for r in ev.reasons)


# ------------------------------------------------------- buckets, ranking, prices


@pytest.mark.parametrize(
    ("value", "label"), [(-1.0, "a"), (0.99, "a"), (1.0, "b"), (1.99, "b"), (2.0, "c"), (9, "c")]
)
def test_bucket(value, label):
    assert bucket(value, (1.0, 2.0), ("a", "b", "c")) == label


def test_bucket_rejects_nan():
    with pytest.raises(ValueError):
        bucket(float("nan"), (1.0, 2.0), ("a", "b", "c"))


def test_rank_by_probability_then_momentum():
    base = evaluate(bars(pullback())).candidate
    a = ScoredCandidate(dataclasses.replace(base, symbol="A", momentum=0.10), 0.72)
    b = ScoredCandidate(dataclasses.replace(base, symbol="B", momentum=0.30), 0.72)
    c = ScoredCandidate(dataclasses.replace(base, symbol="C", momentum=0.01), 0.80)
    assert [s.candidate.symbol for s in rank_candidates([a, b, c])] == ["C", "B", "A"]


def test_limit_price_and_initial_stop():
    assert limit_price(100.0, CFG) == pytest.approx(100.20)
    assert limit_price(123.456, CFG) == pytest.approx(123.70)
    assert initial_stop(100.0, 2.0, CFG) == pytest.approx(96.0)


@pytest.mark.parametrize(("entry", "atr"), [(100.0, 0.0), (100.0, float("nan")), (3.0, 2.0)])
def test_initial_stop_rejects_invalid(entry, atr):
    with pytest.raises(ValueError):
        initial_stop(entry, atr, CFG)


# ------------------------------------------------------- stop management


def pos(**kw):
    base = dict(
        symbol="AAPL",
        entry_price=100.0,
        initial_stop=96.0,
        stop=96.0,
        shares=100,
        days_held=3,
        partial_taken=False,
    )
    base.update(kw)
    return PositionState(**base)


def test_stop_unchanged_below_one_r():
    assert update_stop(pos(), close=102.0, atr=2.0, ema=99.0, cfg=CFG) == 96.0


def test_no_trailing_before_one_r():
    # Trail (ema 102.9 - 0.05) would be 102.85, but +0.75R has not reached breakeven yet.
    assert update_stop(pos(), close=103.0, atr=0.5, ema=102.9, cfg=CFG) == 96.0


def test_breakeven_at_one_r():
    assert update_stop(pos(), close=104.0, atr=2.0, ema=99.0, cfg=CFG) == pytest.approx(100.0)


def test_trailing_uses_higher_of_atr_and_ema():
    # close - 3*ATR = 104; EMA - 0.1*ATR = 105.8
    assert update_stop(pos(), close=110.0, atr=2.0, ema=106.0, cfg=CFG) == pytest.approx(105.8)


def test_stop_only_moves_up():
    p = pos(stop=105.8)
    assert update_stop(p, close=107.0, atr=2.0, ema=104.0, cfg=CFG) == pytest.approx(105.8)


def test_trail_level_above_close_is_ignored():
    p = pos(stop=100.0)
    # EMA - buffer = 102.99 is above the close; only close - 3*ATR = 100.7 is usable.
    assert update_stop(p, close=101.0, atr=0.1, ema=103.0, cfg=CFG) == pytest.approx(100.7)


# ------------------------------------------------------- exits


def test_partial_profit_at_two_r():
    assert exit_actions(pos(), close=108.0, sma_fast=95.0, cfg=CFG) == [
        ExitAction("partial_profit", 50)
    ]


def test_partial_only_once_and_needs_two_shares():
    assert exit_actions(pos(partial_taken=True), close=108.0, sma_fast=95.0, cfg=CFG) == []
    assert exit_actions(pos(shares=1), close=108.0, sma_fast=95.0, cfg=CFG) == []


def test_trend_break_exits_all():
    assert exit_actions(pos(), close=101.0, sma_fast=102.0, cfg=CFG) == [
        ExitAction("trend_break", 100)
    ]


def test_full_exit_wins_over_partial():
    assert exit_actions(pos(), close=108.0, sma_fast=110.0, cfg=CFG) == [
        ExitAction("trend_break", 100)
    ]


@pytest.mark.parametrize(
    ("days", "close", "expected"),
    [(15, 101.0, [ExitAction("time_stop", 100)]), (14, 101.0, []), (15, 102.0, [])],
)
def test_time_stop(days, close, expected):
    assert exit_actions(pos(days_held=days), close=close, sma_fast=95.0, cfg=CFG) == expected


# ------------------------------------------------------- vectorized scan (backtester)


@pytest.mark.parametrize("seed", [1, 5, 9])
def test_entry_table_matches_evaluate_entry_on_every_prefix(seed):
    """The backtester's one-pass scan must equal evaluate_entry on each prefix (no look-ahead)."""
    rng = np.random.default_rng(seed)
    n = 330
    df = bars(100 * np.cumprod(1 + rng.normal(0.0015, 0.015, n)), spread=0.008)
    spy = bars(100 * np.cumprod(1 + rng.normal(0.0005, 0.008, n)), spread=0.004)
    table = entry_table(
        compute_features(df, CFG), regime_table(compute_features(spy, CFG), CFG), CFG
    )
    hits = 0
    for i in range(190, n):
        regime = market_regime(spy.iloc[: i + 1], CFG)
        ev = evaluate_entry("X", df.iloc[: i + 1], regime, CFG, earnings_in_blackout=False)
        assert bool(table["setup"].iloc[i]) == (ev.candidate is not None), (i, ev.reasons)
        if ev.candidate is not None:
            hits += 1
            assert table["signature"].iloc[i] == ev.candidate.signature.key()
    assert hits > 0


def test_entry_table_fails_closed_on_missing_regime_dates():
    df = bars(pullback())
    reg = regime_table(compute_features(SPY.iloc[:-1], CFG), CFG)
    table = entry_table(compute_features(df, CFG), reg, CFG)
    assert not table["regime_ok"].iloc[-1]
    assert not table["setup"].iloc[-1]


def test_regime_table_last_row_matches_market_regime():
    reg = regime_table(compute_features(SPY, CFG), CFG)
    r = market_regime(SPY, CFG)
    assert (bool(reg["ok"].iloc[-1]), reg["trend"].iloc[-1], reg["vol_bucket"].iloc[-1]) == (
        r.ok,
        r.trend,
        r.vol_bucket,
    )


# ------------------------------------------------------- mean-reversion exit set (docs/03)

MR = StrategyConfig(exit_set="mean_reversion")


def test_mean_reversion_partial_when_close_above_sma5_in_profit():
    # +0.25R only, but the close is back above the 5-day SMA: take the first 50%.
    acts = exit_actions(pos(), close=101.0, sma_fast=95.0, cfg=MR, sma_exit=100.5)
    assert acts == [ExitAction("partial_profit", 50)]


def test_mean_reversion_partial_at_one_r():
    acts = exit_actions(pos(), close=104.0, sma_fast=95.0, cfg=MR, sma_exit=110.0)
    assert acts == [ExitAction("partial_profit", 50)]


def test_mean_reversion_needs_a_profit():
    # Above the SMA5 but still below entry: no "target" yet.
    assert exit_actions(pos(), close=99.0, sma_fast=95.0, cfg=MR, sma_exit=98.0) == []


def test_default_exit_set_ignores_sma5():
    assert exit_actions(pos(), close=101.0, sma_fast=95.0, cfg=CFG, sma_exit=100.5) == []


def test_mean_reversion_requires_sma_exit():
    with pytest.raises(ValueError):
        exit_actions(pos(), close=101.0, sma_fast=95.0, cfg=MR)


def test_features_include_exit_sma():
    f = compute_features(bars(trend()), CFG)
    assert f["sma_exit"].iloc[-1] == pytest.approx(f["close"].iloc[-5:].mean())


def test_exit_set_validated():
    from bot.config import ConfigError

    with pytest.raises(ConfigError):
        StrategyConfig(exit_set="yolo")


MRF = StrategyConfig(exit_set="mean_reversion_full")


def test_mean_reversion_full_exits_everything_at_first_target():
    acts = exit_actions(pos(), close=101.0, sma_fast=95.0, cfg=MRF, sma_exit=100.5)
    assert acts == [ExitAction("profit_target", 100)]
    assert exit_actions(pos(), close=104.0, sma_fast=95.0, cfg=MRF, sma_exit=110.0) == [
        ExitAction("profit_target", 100)
    ]


def test_mean_reversion_full_holds_losers_for_stop_or_time():
    assert exit_actions(pos(), close=99.0, sma_fast=95.0, cfg=MRF, sma_exit=98.0) == []
