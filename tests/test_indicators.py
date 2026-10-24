# Author: Kai0421
"""Tests for bot.indicators. Expected values are worked out by hand in the comments."""

import math

import numpy as np
import pandas as pd
import pytest

from bot.indicators import atr, ema, rsi, sma, true_range


def s(values, start="2026-10-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx, dtype="float64")


def assert_series(actual, expected, places=9):
    assert len(actual) == len(expected)
    for a, e in zip(actual.tolist(), expected, strict=True):
        if e is None:
            assert math.isnan(a), f"expected NaN, got {a}"
        else:
            assert a == pytest.approx(e, abs=10**-places)


# ---------------------------------------------------------------- SMA


def test_sma_basic():
    assert_series(sma(s([1, 2, 3, 4, 5]), 3), [None, None, 2, 3, 4])


def test_sma_keeps_index_and_names_result():
    close = s([1, 2, 3, 4, 5])
    out = sma(close, 3)
    assert out.index.equals(close.index)
    assert out.name == "sma_3"


# ---------------------------------------------------------------- EMA


def test_ema_seeded_with_sma_then_smoothed():
    # period 3 -> alpha = 2 / (3 + 1) = 0.5
    # seed = mean(2, 4, 6) = 4; then 0.5*8 + 0.5*4 = 6; 0.5*12 + 0.5*6 = 9
    assert_series(ema(s([2, 4, 6, 8, 12]), 3), [None, None, 4, 6, 9])


def test_ema_period_one_equals_input():
    assert_series(ema(s([3, 1, 4, 1, 5]), 1), [3, 1, 4, 1, 5])


def test_ema_of_constant_is_constant():
    out = ema(s([7.0] * 30), 20)
    assert out.iloc[19:].tolist() == [7.0] * 11


# ---------------------------------------------------------------- RSI (Wilder)


def test_rsi_wilder_period_2_hand_computed():
    # closes 10, 11, 10, 12, 12, 9 -> changes +1, -1, +2, 0, -3
    # idx2: avg_gain = (1+0)/2 = .5, avg_loss = (0+1)/2 = .5            -> RSI 50
    # idx3: gain 2: g = (.5*1 + 2)/2 = 1.25, l = (.5*1 + 0)/2 = .25     -> RS 5, RSI 83.333
    # idx4: flat:   g = .625, l = .125                                   -> RSI 83.333
    # idx5: -3:     g = .3125, l = (.125 + 3)/2 = 1.5625                 -> RS .2, RSI 16.667
    out = rsi(s([10, 11, 10, 12, 12, 9]), 2)
    assert_series(out, [None, None, 50, 100 - 100 / 6, 100 - 100 / 6, 100 - 100 / 1.2])
    assert out.name == "rsi_2"


def test_rsi_all_gains_is_100_and_all_losses_is_0():
    assert rsi(s([1, 2, 3, 4, 5]), 2).iloc[-1] == 100
    assert rsi(s([5, 4, 3, 2, 1]), 2).iloc[-1] == 0


def test_rsi_flat_prices_is_neutral_50():
    assert rsi(s([10] * 6), 2).iloc[-1] == 50


def test_rsi_stays_in_bounds_on_random_walk():
    rng = np.random.default_rng(42)
    close = s(100 + np.cumsum(rng.normal(0, 1, 500)))
    out = rsi(close, 14).dropna()
    assert len(out) == 500 - 14
    assert out.between(0, 100).all()


# ---------------------------------------------------------------- True range / ATR


def test_true_range_uses_previous_close_for_gaps():
    high = s([10, 15, 12])
    low = s([8, 14, 9])
    close = s([9, 14.5, 10])
    # bar0: no previous close -> high - low = 2
    # bar1: gap up: max(1, |15-9|=6, |14-9|=5) = 6
    # bar2: gap down: max(3, |12-14.5|=2.5, |9-14.5|=5.5) = 5.5
    assert_series(true_range(high, low, close), [2, 6, 5.5])


def test_atr_wilder_period_3_hand_computed():
    high = s([10, 11, 12, 11])
    low = s([8, 9, 10, 8])
    close = s([9, 10, 11, 9])
    # TR = 2, 2, 2, 3 ; seed ATR at idx2 = mean(2, 2, 2) = 2 ; idx3 = (2*2 + 3)/3
    out = atr(high, low, close, 3)
    assert_series(out, [None, None, 2, 7 / 3])
    assert out.name == "atr_3"


def test_atr_accepts_ohlc_frame_columns():
    df = pd.DataFrame(
        {"high": [10, 11, 12, 11], "low": [8, 9, 10, 8], "close": [9, 10, 11, 9]},
        index=pd.bdate_range("2026-10-01", periods=4),
        dtype="float64",
    )
    out = atr(df["high"], df["low"], df["close"], 3)
    assert out.index.equals(df.index)


# ---------------------------------------------------------------- input validation


@pytest.mark.parametrize("fn", [sma, ema, rsi])
@pytest.mark.parametrize("period", [0, -1, 2.5])
def test_bad_period_rejected(fn, period):
    with pytest.raises(ValueError):
        fn(s([1, 2, 3, 4]), period)


@pytest.mark.parametrize("fn", [sma, ema, rsi])
def test_nan_input_rejected(fn):
    with pytest.raises(ValueError):
        fn(s([1, 2, float("nan"), 4]), 2)


def test_atr_rejects_misaligned_inputs():
    with pytest.raises(ValueError):
        atr(s([10, 11, 12]), s([8, 9]), s([9, 10, 11]), 2)


def test_atr_rejects_high_below_low():
    with pytest.raises(ValueError):
        atr(s([10, 8]), s([9, 9]), s([9.5, 8.5]), 1)


def test_short_series_gives_all_nan():
    assert sma(s([1, 2]), 5).isna().all()
    assert ema(s([1, 2]), 5).isna().all()
    assert rsi(s([1, 2]), 5).isna().all()
    assert atr(s([2, 3]), s([1, 2]), s([1.5, 2.5]), 5).isna().all()


def test_inputs_are_not_mutated():
    close = s([10, 11, 10, 12, 12, 9])
    before = close.copy()
    sma(close, 2), ema(close, 2), rsi(close, 2)
    pd.testing.assert_series_equal(close, before)
