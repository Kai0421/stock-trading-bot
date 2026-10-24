# Author: Kai0421
"""Tests for the opt-in Kronos strategy and its forecast cache (no torch needed)."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest import BacktestConfig, run_backtest
from bot.config import ConfigError, RiskConfig, Settings
from bot.kronos import (
    DEFAULT_MODEL,
    Forecast,
    append_forecasts,
    forecast_symbol,
    read_forecasts,
    summarize,
)
from bot.strategies import (
    ExitAction,
    PositionState,
    get_strategy,
    strategy_config,
    strategy_for,
)
from bot.strategies.kronos_rank import KronosConfig, KronosRank
from test_strategies import market


def write_cache(path, bars, p_up=0.9, fret=0.03, every=7, symbols=None):
    rows = []
    for sym, df in bars.items():
        if sym == "SPY" or (symbols is not None and sym not in symbols):
            continue
        for ts in df.index[::every]:
            rows.append(Forecast(sym, ts.date(), DEFAULT_MODEL, 0, p_up, fret))
    append_forecasts(path, rows)
    return rows


def strategy(path, **kw):
    return KronosRank(KronosConfig(forecast_path=str(path), **kw))


# ------------------------------------------------------------------ cache


def test_cache_round_trip_and_filters_model_and_seed(tmp_path):
    path = tmp_path / "f.csv"
    assert read_forecasts(path).empty
    append_forecasts(path, [Forecast("AAA", date(2026, 1, 2), DEFAULT_MODEL, 0, 0.7, 0.02)])
    append_forecasts(path, [Forecast("AAA", date(2026, 1, 2), "other", 0, 0.1, -0.05)])
    append_forecasts(path, [Forecast("AAA", date(2026, 1, 2), DEFAULT_MODEL, 1, 0.2, -0.01)])
    df = read_forecasts(path)
    assert df.loc[("AAA", date(2026, 1, 2))].to_dict() == {"p_up": 0.7, "fret": 0.02}
    assert len(df) == 1
    assert read_forecasts(path, seed=1).loc[("AAA", date(2026, 1, 2)), "p_up"] == 0.2


def test_summarize():
    assert summarize(np.array([110.0, 90.0, 120.0, 100.0]), 100.0) == pytest.approx((0.5, 0.05))
    with pytest.raises(ValueError):
        summarize(np.array([np.nan]), 100.0)
    with pytest.raises(ValueError):
        summarize(np.array([]), 100.0)


def test_forecast_symbol_only_sees_bars_up_to_the_day():
    bars, _ = market(n=60, n_symbols=1)
    df = bars["S0"]
    seen = []

    def predict(window, future, samples, seed):
        seen.append((window.index[-1], len(window), len(future), samples, seed))
        return np.full(samples, window["close"].iloc[-1] * 1.02)

    def future_days(day, n):
        return pd.bdate_range(pd.Timestamp(day) + pd.Timedelta(days=1), periods=n)

    days = [df.index[5].date(), df.index[30].date(), df.index[59].date(), date(1999, 1, 1)]
    rows = forecast_symbol("S0", df, days, predict, future_days, lookback=20, samples=4, seed=3)
    # day 5 has too little history and 1999 isn't a bar: both are skipped, not guessed
    assert [r.day for r in rows] == [df.index[30].date(), df.index[59].date()]
    assert seen[0] == (df.index[30], 20, 5, 4, 3)
    assert rows[0].p_up == 1.0 and rows[0].fret == pytest.approx(0.02)


# ------------------------------------------------------------------ strategy


def test_registered_and_selectable_but_not_the_default():
    assert Settings.from_env({}).strategy_name != "kronos_rank"
    assert isinstance(strategy_for(Settings.from_env({"STRATEGY": "kronos_rank"})), KronosRank)
    assert isinstance(get_strategy("kronos_rank"), KronosRank)


def test_no_forecasts_means_no_setups(tmp_path):
    bars, _ = market()
    _, tables = strategy(tmp_path / "missing.csv").prepare(bars, "SPY")
    assert not any(t["setup"].any() for t in tables.values())


def test_setups_follow_the_cached_forecasts(tmp_path):
    bars, _ = market()
    path = tmp_path / "f.csv"
    write_cache(path, bars, symbols={"S0"})
    write_cache(path, bars, p_up=0.55, symbols={"S1"})  # below min_p_up
    write_cache(path, bars, fret=0.001, symbols={"S2"})  # below min_fret
    _, tables = strategy(path).prepare(bars, "SPY")
    assert tables["S0"]["setup"].any()
    for sym in ("S1", "S2", "S3"):
        assert not tables[sym]["setup"].any()
    t = tables["S0"]
    on = t[t["setup"]]
    assert all(s.startswith("kronos_rank|") for s in on["signature"])
    assert list(on["score"]) == pytest.approx([0.03] * len(on))
    # a setup can only be on a bar that has its own forecast
    cached = {r[1] for r in read_forecasts(path).loc[["S0"]].index}
    assert {ts.date() for ts in on.index} <= cached


def test_setups_have_no_look_ahead(tmp_path):
    bars, _ = market()
    path = tmp_path / "f.csv"
    write_cache(path, bars, every=3)
    s = strategy(path)
    _, full = s.prepare(bars, "SPY")
    cut = bars["SPY"].index[450]
    _, prefix = s.prepare({k: v.loc[:cut] for k, v in bars.items()}, "SPY")
    for sym in bars:
        pd.testing.assert_frame_equal(prefix[sym], full[sym].loc[:cut])


def test_config_refuses_bearish_or_stopless_settings():
    with pytest.raises(ConfigError):
        KronosConfig(stop_atr_mult=0)
    with pytest.raises(ConfigError):
        KronosConfig(min_p_up=0.4)
    with pytest.raises(ConfigError):
        KronosConfig(min_fret=-0.01)
    with pytest.raises(ConfigError):
        KronosConfig(p_up_edges=(0.9, 0.8))


def test_exit_is_the_time_stop():
    s = KronosRank()
    p = PositionState("S0", 100.0, 95.0, 95.0, 10, 1, False)
    assert s.manage(p, {"close": 120.0}) == ([], 95.0)
    late = dataclasses.replace(p, days_held=5)
    assert s.manage(late, {"close": 90.0})[0] == [ExitAction("time_stop", 10)]
    assert s.stop_for(100.0, 2.0) == pytest.approx(95.0)


def test_backtest_runs_end_to_end(tmp_path):
    bars, sectors = market()
    path = tmp_path / "f.csv"
    write_cache(path, bars, every=2)
    bt = BacktestConfig(start=date(2018, 1, 1), end=date(2020, 12, 31), gated=False)
    res = run_backtest(bars, sectors, strategy(path), RiskConfig(), bt)
    assert res.strategy == "kronos_rank"
    assert res.signal_trades
    assert {t.exit_reason for t in res.signal_trades} <= {"stop", "time_stop", "end_of_data"}


# ------------------------------------------------------------------ settings


def test_settings_feed_the_strategy_config(tmp_path):
    env = {
        "STRATEGY": "kronos_rank",
        "KRONOS_FORECASTS": str(tmp_path / "k.csv"),
        "KRONOS_MIN_P_UP": "0.7",
        "KRONOS_MIN_FRET": "0.02",
    }
    s = strategy_for(Settings.from_env(env))
    assert isinstance(s, KronosRank)
    assert s.config.forecast_path == str(tmp_path / "k.csv")
    assert (s.config.min_p_up, s.config.min_fret) == (0.7, 0.02)
    # unset keeps the defaults
    assert strategy_for(Settings.from_env({"STRATEGY": "kronos_rank"})).config == KronosConfig()
    # the same values reach the backtester and the other tools
    assert strategy_config("kronos_rank", Settings.from_env(env)) == s.config
    assert strategy_config("rsi2_reversion", Settings.from_env(env)) is None


def test_settings_reject_bad_kronos_values():
    with pytest.raises(ConfigError, match="KRONOS_MIN_P_UP"):
        Settings.from_env({"KRONOS_MIN_P_UP": "high"})
    with pytest.raises(ConfigError, match="min_p_up"):
        Settings.from_env({"KRONOS_MIN_P_UP": "0.3"})
    with pytest.raises(ConfigError, match="min_fret"):
        Settings.from_env({"KRONOS_MIN_FRET": "-0.1"})
