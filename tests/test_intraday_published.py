# Author: Kai0421
"""Tests for the published intraday strategies (bot.intraday.published)."""

from datetime import time

import numpy as np
import pandas as pd
import pytest

from bot.backtest import Trade
from bot.config import ConfigError, RiskConfig
from bot.intraday import intraday_registry
from bot.intraday.backtest import _cost_line
from bot.intraday.base import IntradayPosition, session_features
from bot.intraday.published import (
    NoiseBreakout,
    NoiseConfig,
    OrbInPlay,
    OrbInPlayConfig,
    VwapTrend,
    session_vwap,
)

DAYS = pd.bdate_range("2026-09-01", periods=20)


def session(day, closes, volume=1e5, spread=0.0005):
    c = np.asarray(closes, dtype="float64")
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range(day + pd.Timedelta("9h30min"), periods=len(c), freq="5min")
    v = np.broadcast_to(np.asarray(volume, dtype="float64"), c.shape)
    hi = np.maximum(o, c) * (1 + spread)
    lo = np.minimum(o, c) * (1 - spread)
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": v}, index=idx)


def quiet_history(n=15, price=100.0):
    """n sessions wobbling +-0.1% around a flat price."""
    wobble = price * (1 + 0.001 * np.sin(np.arange(78)))
    return [session(d, wobble) for d in DAYS[:n]]


def test_session_vwap_resets_each_day():
    b = pd.concat([session(DAYS[0], np.full(78, 100.0)), session(DAYS[1], np.full(78, 50.0))])
    v = session_vwap(session_features(b))
    assert v.iloc[77] == pytest.approx(100.0, rel=1e-3)
    assert v.iloc[78] == pytest.approx(50.0, rel=1e-3)


def test_noise_breakout_buys_a_half_hour_close_above_the_area():
    trend = np.r_[np.full(6, 100.0), np.linspace(100, 101.5, 72)]  # rises after 10:00
    b = pd.concat([*quiet_history(), session(DAYS[15], trend)])
    s = NoiseBreakout()
    f = s.prepare(b, None, None)
    hits = f.index[f["setup"]]
    assert len(hits) == 1 and hits[0].date() == DAYS[15].date()
    assert (hits[0].minute + 5) % 30 == 0  # only on bars ending on the half hour
    bar = f.loc[hits[0]].to_dict()
    assert bar["close"] > bar["upper"]
    assert 0 < s.stop_for(bar["close"], bar) < bar["close"]


def test_setup_signatures_are_strings_when_a_bucket_is_unknown():
    # no daily bars, so the gap bucket is unknown; the report needs a string anyway
    trend = np.r_[np.full(6, 100.0), np.linspace(100, 101.5, 72)]
    b = pd.concat([*quiet_history(), session(DAYS[15], trend)])
    f = NoiseBreakout().prepare(b, None, None)
    sigs = f.loc[f["setup"], "signature"].tolist()
    assert sigs and all(isinstance(s, str) for s in sigs)
    assert sigs[0].startswith("noise_breakout|") and sigs[0].endswith("|na")


def test_noise_area_uses_only_earlier_sessions():
    a = pd.concat([*quiet_history(), session(DAYS[15], np.full(78, 100.0))])
    b = pd.concat([*quiet_history(), session(DAYS[15], np.linspace(100, 90, 78))])
    ua = NoiseBreakout().features(a, None, None)["upper"].iloc[-78:]
    ub = NoiseBreakout().features(b, None, None)["upper"].iloc[-78:]
    # same sigma (from earlier days), same open: same band all day
    np.testing.assert_allclose(ua.to_numpy(), ub.to_numpy())


def test_noise_breakout_exits_only_on_half_hour_checks():
    s = NoiseBreakout()
    pos = IntradayPosition("SPY", 101.0, 100.0, 100.0, 1, 1)
    bar = {"close": 100.2, "upper": 100.5, "vwap": 100.3, "check": False}
    assert s.on_bar(pos, bar)[0] is None
    assert s.on_bar(pos, {**bar, "check": True})[0] == "below_trail"


def test_vwap_trend_enters_above_and_exits_below_vwap():
    closes = np.r_[[100.0, 99.8, 99.7, 100.4], np.full(74, 100.5)]
    b = pd.concat([*quiet_history(), session(DAYS[15], closes)])
    s = VwapTrend()
    f = s.prepare(b, None, None)
    today = f[f["day"] == DAYS[15]]
    assert today.index[today["setup"]][0] == DAYS[15] + pd.Timedelta("9h45min")
    pos = IntradayPosition("QQQ", 100.4, 99.9, 99.9, 1, 1)
    assert s.on_bar(pos, {"close": 100.0, "vwap": 100.1}) == ("below_vwap", 99.9)


def test_orb_in_play_needs_an_up_first_bar_on_high_relative_volume():
    hist = quiet_history()
    up = session(DAYS[15], np.r_[[100.0, 100.0], np.full(76, 100.5)])
    up.iloc[0, up.columns.get_loc("close")] = 100.4  # first bar closes up
    up.iloc[0, up.columns.get_loc("volume")] = 5e5  # 5x the usual opening volume
    s = OrbInPlay(OrbInPlayConfig(min_daily_atr=0.0))
    daily = pd.DataFrame(
        {"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 1e6},
        index=pd.bdate_range(end=DAYS[14], periods=30),
    )
    f = s.prepare(pd.concat([*hist, up]), daily, None)
    hits = f.index[f["setup"]]
    assert list(hits) == [DAYS[15] + pd.Timedelta("9h30min")]
    assert f.loc[hits[0], "rvol"] == pytest.approx(5.0)
    bar = f.loc[hits[0]].to_dict()
    assert s.stop_for(100.5, bar) == pytest.approx(100.5 - 0.1 * bar["daily_atr"], abs=0.011)
    # quiet volume: no setup
    quiet = up.copy()
    quiet.iloc[0, quiet.columns.get_loc("volume")] = 5e4
    assert not s.prepare(pd.concat([*hist, quiet]), daily, None)["setup"].any()


def test_cost_line_adds_costs_back():
    risk = RiskConfig()
    t = Trade("SPY", "x", DAYS[0], DAYS[0], DAYS[0], 100.05, 99.0, 1, -0.10, -0.095, "flatten")
    line = _cost_line([t], risk)
    assert "-0.100% of price after costs" in line and "before" in line


def test_published_strategies_are_registered_and_validated():
    assert {"noise_breakout", "vwap_trend", "orb_in_play"} <= set(intraday_registry())
    with pytest.raises(ConfigError):
        NoiseConfig(check_minutes=7)
    with pytest.raises(ConfigError):
        OrbInPlayConfig(stop_daily_atr=0)
    with pytest.raises(ConfigError):
        NoiseConfig(flatten_at=time(16, 0))
