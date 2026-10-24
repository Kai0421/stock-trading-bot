# Author: Kai0421
"""Tests for intraday mode: the TRADING_MODE switch, intraday strategies and backtester."""

from datetime import date, time

import numpy as np
import pandas as pd
import pytest

from bot.backtest import Trade
from bot.config import ConfigError, RiskConfig, Settings
from bot.intraday import get_intraday_strategy, intraday_registry
from bot.intraday.backtest import (
    IntradayBacktestConfig,
    Signal,
    evaluate,
    perturbations,
    run_portfolio,
    simulate_symbol,
)
from bot.intraday.base import IntradayPosition, attach_daily, session_features
from bot.intraday.strategies import GapFill, GapFillConfig, OrbBreakout

DAY = pd.Timestamp("2026-10-05")  # a Monday
PREV = pd.Timestamp("2026-10-02")


def session(day, closes, opens=None, spread=0.001):
    """5-minute regular-hours bars stamped at their start, from 09:30."""
    c = np.asarray(closes, dtype="float64")
    o = np.r_[c[0], c[:-1]] if opens is None else np.asarray(opens, dtype="float64")
    idx = pd.date_range(pd.Timestamp(day) + pd.Timedelta("9h30min"), periods=len(c), freq="5min")
    hi = np.maximum(o, c) * (1 + spread)
    lo = np.minimum(o, c) * (1 - spread)
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": 1e5}, index=idx)


def daily(end=PREV, n=300, start=50.0, last=100.0):
    """Daily bars in a steady uptrend ending at ``last`` on ``end``."""
    idx = pd.bdate_range(end=end, periods=n)
    c = np.geomspace(start, last, n)
    return pd.DataFrame(
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx
    )


SPY = daily(last=500.0, start=300.0)


# ------------------------------------------------------------------ config


def test_trading_mode_defaults_to_swing():
    s = Settings.from_env({})
    assert s.trading_mode == "swing" and s.strategy_name == "trend_pullback"


def test_intraday_mode_defaults_to_an_intraday_strategy():
    s = Settings.from_env({"TRADING_MODE": "intraday"})
    assert s.trading_mode == "intraday" and s.strategy_name in intraday_registry()


def test_mode_and_strategy_must_match():
    with pytest.raises(ConfigError, match="intraday STRATEGY"):
        Settings.from_env({"TRADING_MODE": "intraday", "STRATEGY": "trend_pullback"})
    with pytest.raises(ConfigError, match="STRATEGY must be one of"):
        Settings.from_env({"TRADING_MODE": "swing", "STRATEGY": "orb_breakout"})
    with pytest.raises(ConfigError, match="TRADING_MODE"):
        Settings.from_env({"TRADING_MODE": "scalping"})


def test_intraday_mode_keeps_the_same_risk_limits():
    s = Settings.from_env({"TRADING_MODE": "intraday", "RISK_PER_TRADE": "0.0025"})
    assert s.risk.risk_per_trade == 0.0025
    with pytest.raises(ConfigError):
        Settings.from_env({"TRADING_MODE": "intraday", "RISK_PER_TRADE": "0.05"})


def test_flatten_time_must_be_before_the_close():
    with pytest.raises(ConfigError):
        GapFillConfig(flatten_at=time(16, 0))
    with pytest.raises(ConfigError):
        GapFillConfig(entry_end=time(16, 30))


# ------------------------------------------------------------------ features


def test_session_features_and_previous_day_context():
    bars = pd.concat([session(PREV, [100 + i * 0.1 for i in range(78)]),
                      session(DAY, [101 - i * 0.1 for i in range(78)])])  # fmt: skip
    f = attach_daily(session_features(bars), daily(end=DAY), SPY)
    today = f[f["day"] == DAY]
    assert today["bar_no"].iloc[0] == 0 and today["minute"].iloc[0] == 570
    assert today["day_high"].is_monotonic_increasing
    assert today["day_low"].is_monotonic_decreasing
    # prev_close is Friday's daily close, never Monday's own daily bar (no look-ahead).
    d = daily(end=DAY)
    assert today["prev_close"].iloc[0] == pytest.approx(d["close"].iloc[-2])
    changed = d.copy()
    changed.iloc[-1] = changed.iloc[-1] * 2
    again = attach_daily(session_features(bars), changed, SPY)
    assert np.allclose(again["prev_close"], f["prev_close"], equal_nan=True)


def with_prev(today):
    """Prepend Friday's session (flat at 100) so bar indicators are warmed up."""
    return pd.concat([session(PREV, [100.0 + 0.1 * (i % 2) for i in range(78)]), today])


def orb_day():
    """Range 100-101 for 30 minutes, then a first close above it at 10:05."""
    closes = [100.5, 100.9, 100.2, 100.8, 100.3, 100.6, 100.7, 101.4, 101.6, 101.2]
    closes += [101.5] * 68
    return with_prev(session(DAY, closes))


def test_orb_setup_is_the_first_close_above_the_range():
    bars = orb_day()
    f = OrbBreakout().prepare(bars, daily(), SPY)
    setups = f.index[f["setup"]]
    assert list(setups) == [DAY + pd.Timedelta("10h05min")]
    assert f.loc[setups[0], "signature"].startswith("orb_breakout|")
    assert f["or_high"].iloc[:6].isna().all()  # the range isn't known until it closes


def test_no_setup_without_a_daily_uptrend():
    down = daily(start=150.0, last=100.0)
    assert not OrbBreakout().prepare(orb_day(), down, SPY)["setup"].any()


def gap_day(fill=True):
    """Prev close 100; opens at 98.5 (1.5% gap down), dips, reclaims the open at 09:50."""
    closes = [98.3, 98.1, 98.2, 98.4, 98.7, 99.0, 99.4]
    closes += [100.2] * 3 if fill else [98.8] * 3
    closes += [closes[-1]] * (78 - len(closes))
    opens = [98.5, *closes[:-1]]
    return with_prev(session(DAY, closes, opens))


def test_gap_fill_setup_and_exit_at_the_previous_close():
    s = GapFill()
    f = s.prepare(gap_day(), daily(), SPY)
    setups = f.index[f["setup"]]
    assert list(setups) == [DAY + pd.Timedelta("9h50min")]
    (sig,) = simulate_symbol("AAPL", f, s, RiskConfig())
    assert sig.trade.exit_reason == "gap_filled"
    assert sig.trade.entry_date == DAY + pd.Timedelta("9h55min")
    assert sig.trade.win


def test_entries_outside_the_window_are_masked():
    late = GapFill(GapFillConfig(entry_start=time(10, 30), entry_end=time(11, 0)))
    assert not late.prepare(gap_day(), daily(), SPY)["setup"].any()


@pytest.mark.parametrize("name", sorted(intraday_registry()))
def test_no_look_ahead(name):
    rng = np.random.default_rng(7)
    days = [pd.Timestamp("2026-09-30"), pd.Timestamp("2026-10-01"), PREV]
    bars = pd.concat([session(d, 100 * np.exp(np.cumsum(rng.normal(0, 0.003, 78)))) for d in days])
    s = get_intraday_strategy(name)
    full = s.prepare(bars, daily(end=PREV), SPY)
    for k in (40, 100, 200):
        part = s.prepare(bars.iloc[:k], daily(end=PREV), SPY)
        cols = ["setup", "signature"]
        pd.testing.assert_frame_equal(full[cols].iloc[:k], part[cols])


# ------------------------------------------------------------------ simulation


def test_stop_and_flatten_exits():
    s = OrbBreakout()
    stopped = orb_day()
    stopped.iloc[78 + 9, stopped.columns.get_loc("low")] = 99.0  # 10:15 trades through the stop
    f = s.prepare(stopped, daily(), SPY)
    (sig,) = simulate_symbol("AAPL", f, s, RiskConfig())
    t = sig.trade
    assert t.exit_reason == "stop" and t.r_multiple < -0.9
    held = s.prepare(orb_day(), daily(), SPY)
    (sig,) = simulate_symbol("AAPL", held, s, RiskConfig())
    assert sig.trade.exit_reason == "flatten"
    assert sig.trade.exit_date == DAY + pd.Timedelta("15h50min")


def test_manage_never_lowers_the_stop():
    s = OrbBreakout()
    pos = IntradayPosition("AAPL", 100.0, 99.0, 99.5, 10, 3)
    _, stop = s.manage(pos, {"close": 99.6, "atr": 0.5})
    assert stop == 99.5
    _, stop = s.manage(pos, {"close": 102.0, "atr": 0.2})  # +2R: breakeven, then trail
    assert stop == pytest.approx(101.6)


def test_stop_for_rejects_stops_at_or_above_entry():
    with pytest.raises(ValueError):
        GapFill().stop_for(100.0, {"day_low": 100.5, "atr": 0.1})


def trade(symbol, minute, exit_minute, pnl=1.0, sig="x|a"):
    t0 = DAY + pd.Timedelta(minutes=minute)
    return Signal(
        Trade(
            symbol,
            sig,
            t0,
            t0,
            DAY + pd.Timedelta(minutes=exit_minute),
            100.0,
            99.0,
            1,
            pnl,
            pnl,
            "flatten",
        ),  # fmt: skip
        0.0,
    )


SECTORS = {s: f"S{i}" for i, s in enumerate("ABCDEFGHIJ")}


def test_sale_proceeds_are_not_reused_the_same_day():
    risk = RiskConfig(max_per_sector=2)
    bt = IntradayBacktestConfig(DAY.date(), DAY.date(), gated=False)
    signals = [trade(s, 600 + 10 * i, 605 + 10 * i) for i, s in enumerate("ABCDEFGHIJ")]
    p = run_portfolio(signals, pd.DatetimeIndex([DAY]), SECTORS, risk, bt)
    spent = sum(t.shares * t.entry_price for t in p.trades)
    assert spent <= 100_000 * (1 - risk.cash_reserve_pct) + 1e-6
    assert len(p.trades) < len(signals)


def test_intraday_daily_loss_halts_new_entries():
    risk = RiskConfig()
    bt = IntradayBacktestConfig(DAY.date(), DAY.date(), gated=False)
    signals = [trade("A", 600, 605, pnl=-20.0), trade("B", 610, 615), trade("C", 620, 625)]
    p = run_portfolio(signals, pd.DatetimeIndex([DAY]), SECTORS, risk, bt)
    assert [t.symbol for t in p.trades] == ["A"]  # A lost over 3%: nothing after it


def test_gate_needs_history_before_the_session():
    bt = IntradayBacktestConfig(DAY.date(), DAY.date())
    p = run_portfolio([trade("A", 600, 605)], pd.DatetimeIndex([DAY]), SECTORS, RiskConfig(), bt)
    assert p.trades == [] and p.gate_rejections == 1


def test_evaluate_and_perturbations():
    s = OrbBreakout()
    f = s.prepare(orb_day(), daily(), SPY)
    signals = simulate_symbol("AAPL", f, s, RiskConfig())
    bt = IntradayBacktestConfig(date(2026, 10, 5), date(2026, 10, 5), gated=False)
    r = evaluate(s, signals, pd.DatetimeIndex([DAY]), {"AAPL": "IT"}, RiskConfig(), bt)
    assert r.signal_metrics.trades == 1 and r.verdict == "REJECTED"  # n < 100
    labels = [label for label, _ in perturbations(s)]
    assert "trail_atr x0.8" in labels


def test_sessions_before_the_daily_history_fail_closed():
    bars = session(pd.Timestamp("2026-09-01"), [100.0] * 78)
    f = attach_daily(session_features(bars), daily(end=DAY, n=5), daily(end=DAY, n=5))
    assert f["prev_close"].isna().all() and not f["regime_ok"].any()
