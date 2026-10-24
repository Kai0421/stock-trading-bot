# Author: Kai0421
"""Tests for the research-report strategies: ibs_reversion, confluence_reversion and
high_momentum (docs/14-research-strategies.md)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest import BacktestConfig, run_backtest
from bot.config import ConfigError, RiskConfig
from bot.strategies import ExitAction, PositionState, get_strategy, registry
from bot.strategies.common import down_streak, internal_bar_strength
from bot.strategies.confluence_reversion import ConfluenceConfig, ConfluenceReversion
from bot.strategies.high_momentum import HighMomentum, MomentumConfig
from bot.strategies.ibs_reversion import IbsConfig, IbsReversion

RISK = RiskConfig()
BT = BacktestConfig(start=date(2018, 1, 1), end=date(2020, 12, 31), gated=False)
NEW = ("ibs_reversion", "confluence_reversion", "high_momentum")


def ohlcv(closes, idx, spread=0.008, volume=2_000_000, seed=0):
    rng = np.random.default_rng(seed)
    c = np.asarray(closes, dtype="float64")
    opens = np.concatenate([[c[0]], c[:-1]])
    top = np.maximum(opens, c)
    bottom = np.minimum(opens, c)
    # Random wicks so the close lands anywhere in the day's range (IBS varies).
    return pd.DataFrame(
        {
            "open": opens,
            "high": top * (1 + spread * rng.uniform(0.1, 1.5, len(c))),
            "low": bottom * (1 - spread * rng.uniform(0.1, 1.5, len(c))),
            "close": c,
            "volume": volume,
        },
        index=idx,
    )


def market(n=700, n_symbols=4, seed=11):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2017-01-02", periods=n)
    bars = {"SPY": ohlcv(100 * np.cumprod(1 + rng.normal(0.0006, 0.008, n)), idx, 0.004, seed=1)}
    sectors = {"SPY": "Broad"}
    for k in range(n_symbols):
        closes = 100 * np.cumprod(1 + rng.normal(0.0012, 0.018, n))
        bars[f"S{k}"] = ohlcv(closes, idx, seed=k + 2)
        sectors[f"S{k}"] = f"X{k}"
    return bars, sectors


def pos(**kw):
    base = dict(
        symbol="X",
        entry_price=100.0,
        initial_stop=94.0,
        stop=94.0,
        shares=100,
        days_held=1,
        partial_taken=False,
    )
    return PositionState(**(base | kw))


# ------------------------------------------------------- helpers


def test_internal_bar_strength():
    idx = pd.bdate_range("2020-01-01", periods=3)
    f = pd.DataFrame(
        {"high": [10.0, 10.0, 10.0], "low": [8.0, 8.0, 10.0], "close": [8.5, 10.0, 10.0]},
        index=idx,
    )
    assert internal_bar_strength(f).tolist() == [0.25, 1.0, 0.5]  # flat bar -> 0.5


def test_down_streak_counts_consecutive_lower_closes():
    close = pd.Series([10, 9, 8, 8, 7, 6, 5, 6, 5.0])
    assert down_streak(close).tolist() == [0, 1, 2, 0, 1, 2, 3, 0, 1]


# ------------------------------------------------------- registry and hard rules


def test_new_strategies_are_registered():
    assert set(NEW) <= set(registry())
    for name in NEW:
        assert get_strategy(name).name == name


@pytest.mark.parametrize("name", NEW)
def test_no_look_ahead_and_prefixed_signatures(name):
    bars, _ = market()
    s = get_strategy(name)
    _, full = s.prepare(bars, "SPY")
    cut = bars["SPY"].index[520]
    _, prefix = s.prepare({k: v.loc[:cut] for k, v in bars.items()}, "SPY")
    for sym in bars:
        pd.testing.assert_frame_equal(prefix[sym], full[sym].loc[:cut])
    sigs = [x for t in full.values() for x in t["signature"].dropna()]
    assert sigs, f"{name} produced no setups on the synthetic market"
    assert all(x.startswith(name + "|") for x in sigs)


@pytest.mark.parametrize("name", NEW)
def test_backtest_runs_end_to_end(name):
    bars, sectors = market()
    res = run_backtest(bars, sectors, get_strategy(name), RISK, BT)
    assert res.strategy == name
    assert res.signal_trades
    for t in res.signal_trades:
        assert 0 < t.initial_stop < t.entry_price


@pytest.mark.parametrize("name", NEW)
def test_stop_is_below_entry(name):
    s = get_strategy(name)
    stop = s.stop_for(100.0, 2.0)
    assert 0 < stop < 100.0
    with pytest.raises(ValueError):
        s.stop_for(100.0, float("nan"))


def test_no_setups_when_spy_is_below_its_200_day_average():
    bars, _ = market()
    idx = bars["SPY"].index
    falling = 100 * np.cumprod(np.full(len(idx), 0.999))
    bars["SPY"] = ohlcv(falling, idx, 0.004)
    for name in NEW:
        _, tables = get_strategy(name).prepare(bars, "SPY")
        assert not any(t["setup"].any() for t in tables.values()), name


# ------------------------------------------------------- IBS reversion


def test_ibs_config_validation():
    with pytest.raises(ConfigError):
        IbsConfig(stop_atr_mult=0)
    with pytest.raises(ConfigError):
        IbsConfig(ibs_entry_below=0.9, ibs_exit_above=0.8)


def test_ibs_setup_needs_low_close_in_range_on_a_down_day():
    bars, _ = market()
    s = IbsReversion()
    features, tables = s.prepare(bars, "SPY")
    for sym, t in tables.items():
        f = features[sym]
        on = t["setup"]
        assert (f.loc[on, "ibs"] < s.config.ibs_entry_below).all()
        assert (f.loc[on, "close"] < f.loc[on, "prev_close"]).all()
        assert (f.loc[on, "close"] > f.loc[on, "sma_trend"]).all()


def test_ibs_exits():
    s = IbsReversion()
    calm = {"close": 99.0, "prev_high": 100.0, "ibs": 0.5}
    assert s.manage(pos(), calm) == ([], 94.0)
    assert s.manage(pos(), calm | {"close": 100.5})[0] == [ExitAction("prev_high_exit", 100)]
    assert s.manage(pos(), calm | {"ibs": 0.85})[0] == [ExitAction("ibs_exit", 100)]
    assert s.manage(pos(days_held=5), calm)[0] == [ExitAction("time_stop", 100)]


# ------------------------------------------------------- confluence reversion


def test_confluence_config_validation():
    with pytest.raises(ConfigError):
        ConfluenceConfig(min_votes=5)
    with pytest.raises(ConfigError):
        ConfluenceConfig(stop_atr_mult=-1)


def test_confluence_votes_and_score():
    bars, _ = market()
    s = ConfluenceReversion()
    features, tables = s.prepare(bars, "SPY")
    seen = set()
    for sym, t in tables.items():
        f = features[sym]
        on = t["setup"]
        assert (f.loc[on, "votes"] >= s.config.min_votes).all()
        seen |= set(f.loc[on, "votes"].astype(int))
        # More votes always rank above fewer votes.
        if on.any():
            by_votes = t.loc[on, "score"].groupby(f.loc[on, "votes"]).agg(["min", "max"])
            lows, highs = by_votes["min"].to_numpy(), by_votes["max"].to_numpy()
            assert (lows[1:] > highs[:-1]).all()
    assert seen and seen <= {2, 3, 4}


def test_confluence_exits():
    s = ConfluenceReversion()
    calm = {"close": 99.0, "sma_exit": 100.0, "prev_high": 101.0}
    assert s.manage(pos(), calm) == ([], 94.0)
    assert s.manage(pos(), calm | {"close": 100.5})[0] == [ExitAction("bounce_exit", 100)]
    assert s.manage(pos(), calm | {"close": 99.5, "sma_exit": 99.8, "prev_high": 99.2})[0] == [
        ExitAction("bounce_exit", 100)
    ]
    assert s.manage(pos(days_held=7), calm)[0] == [ExitAction("time_stop", 100)]


# ------------------------------------------------------- high momentum


def test_momentum_config_validation():
    with pytest.raises(ConfigError):
        MomentumConfig(trail_atr_mult=0)
    with pytest.raises(ConfigError):
        MomentumConfig(breakout_days=300)


def test_momentum_setups_are_near_the_high_after_a_strong_run():
    bars, _ = market()
    s = HighMomentum()
    features, tables = s.prepare(bars, "SPY")
    for sym, t in tables.items():
        f = features[sym]
        on = t["setup"]
        assert (f.loc[on, "close"] >= f.loc[on, "high_52w"] * (1 - s.config.near_high_pct)).all()
        assert (f.loc[on, "momentum"] > s.config.min_momentum).all()
        assert (f.loc[on, "close"] > f.loc[on, "breakout_level"]).all()
        pd.testing.assert_series_equal(t.loc[on, "score"], f.loc[on, "momentum"], check_names=False)


def test_momentum_trails_the_stop_and_exits_on_trend_break():
    s = HighMomentum()
    bar = {"close": 120.0, "atr": 4.0, "sma_exit": 110.0}
    actions, stop = s.manage(pos(), bar)
    assert actions == [] and stop == pytest.approx(108.0)  # 120 - 3 x 4
    # The stop never moves down when the price falls back.
    actions, stop = s.manage(pos(stop=108.0), bar | {"close": 111.0})
    assert actions == [] and stop == 108.0
    assert s.manage(pos(), bar | {"close": 109.0})[0] == [ExitAction("trend_break", 100)]
    assert s.manage(pos(days_held=60), bar)[0] == [ExitAction("time_stop", 100)]
