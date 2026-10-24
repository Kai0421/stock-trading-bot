# Author: Kai0421
"""Tests for the auction-geometry intraday strategies (bot.intraday.auction)."""

from datetime import time

import numpy as np
import pandas as pd
import pytest

from bot.config import ConfigError
from bot.intraday import intraday_registry
from bot.intraday.auction import (
    AuctionConfig,
    IbFailure,
    VaRejection,
    VaRotation,
    initial_balance,
    value_areas,
)
from bot.intraday.base import IntradayPosition, session_features

PREV = pd.Timestamp("2026-10-02")
DAY = pd.Timestamp("2026-10-05")


def bars(day, closes, volume=1e5, spread=0.001, opens=None):
    c = np.asarray(closes, dtype="float64")
    o = np.r_[c[0], c[:-1]] if opens is None else np.asarray(opens, dtype="float64")
    idx = pd.date_range(day + pd.Timedelta("9h30min"), periods=len(c), freq="5min")
    v = np.broadcast_to(np.asarray(volume, dtype="float64"), c.shape)
    hi = np.maximum(o, c) * (1 + spread)
    lo = np.minimum(o, c) * (1 - spread)
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": v}, index=idx)


def balanced_prev():
    """Yesterday: a balanced day rotating 99.2-100.8 around 100, thin tails to 98/102."""
    core = 100 + 0.8 * np.sin(np.linspace(0, 6 * np.pi, 58)) ** 3  # dwells near 100
    closes = np.r_[core[:20], np.linspace(99.5, 98, 5), np.linspace(98, 99.5, 5),
                   core[20:], np.linspace(100.5, 102, 5), np.linspace(102, 100.5, 5)]  # fmt: skip
    vol = np.where(np.abs(closes - 100) < 0.9, 1e6, 1e4)
    return bars(PREV, closes, vol)


def with_today(today):
    return pd.concat([balanced_prev(), today])


def test_value_area_is_yesterdays_and_centred_on_the_poc():
    b = with_today(bars(DAY, np.full(78, 100.0)))
    f = session_features(b)
    va = value_areas(f)
    assert va.loc[f["day"] == PREV].isna().all().all()  # nothing known on day one
    today = va.loc[f["day"] == DAY].iloc[0]
    assert today["val"] < 100 < today["vah"]
    assert today["poc"] == pytest.approx(100, abs=0.1)
    assert today["vah"] - today["val"] < 2  # the low-volume tails are outside


def test_value_area_does_not_use_todays_bars():
    a = with_today(bars(DAY, np.full(78, 100.0)))
    b = with_today(bars(DAY, np.full(78, 120.0)))
    va_a = value_areas(session_features(a)).iloc[-1]
    va_b = value_areas(session_features(b)).iloc[-1]
    pd.testing.assert_series_equal(va_a, va_b)


def test_initial_balance_is_known_only_after_the_first_hour():
    f = session_features(bars(DAY, np.linspace(100, 101, 78)))
    ib = initial_balance(f, 5)
    assert ib["ib_low"].iloc[:11].isna().all()  # bars before the 10:25 bar
    assert ib["ib_low"].iloc[11] == pytest.approx(f["low"].iloc[:12].min())
    assert ib["ib_high"].iloc[20] == pytest.approx(f["high"].iloc[:12].max())


def rejection_day():
    """Opens at 100, probes under VAL to 98.9 by 10:05, then closes back inside."""
    closes = np.r_[np.full(6, 100.0), [99.3, 98.9, 99.2], np.full(69, 99.4)]
    return bars(DAY, closes)


def test_va_rejection_buys_the_close_back_inside_with_a_structural_stop():
    s = VaRejection()
    f = s.prepare(with_today(rejection_day()), None, None)
    hits = f.index[f["setup"]]
    assert len(hits) == 1 and hits[0] == DAY + pd.Timedelta("10h10min")
    bar = f.loc[hits[0]].to_dict()
    stop = s.stop_for(bar["close"], bar)
    assert stop < bar["low"] and stop < 98.9 * 0.999
    assert bar["target"] == pytest.approx(bar["poc"])
    assert f.loc[hits[0], "signature"].startswith("va_rejection|")


def test_va_rejection_needs_enough_reward():
    s = VaRejection(AuctionConfig(min_rr=50))
    f = s.prepare(with_today(rejection_day()), None, None)
    assert not f["setup"].any()


def test_auction_exit_at_target():
    s = VaRejection()
    pos = IntradayPosition("X", 99.2, 98.4, 98.4, 10, 1)
    assert s.on_bar(pos, {"close": 99.9, "target": 100.0})[0] is None
    assert s.on_bar(pos, {"close": 100.0, "target": 100.0}) == ("target", 98.4)


def test_va_rotation_needs_an_open_inside_the_value_area():
    # open just under the POC, dip toward VAL, up bar: setup
    closes = np.r_[np.full(6, 99.7), [99.5, 99.4, 99.6], np.full(69, 99.7)]
    s = VaRotation(AuctionConfig(min_risk_atr=0.1, min_rr=0.5))
    f = s.prepare(with_today(bars(DAY, closes)), None, None)
    assert f["setup"].sum() == 1
    # the same day opening above the POC is not a rotation long
    f2 = s.prepare(with_today(bars(DAY, closes + 1.0)), None, None)
    assert not f2["setup"].any()


def test_ib_failure_needs_a_probe_after_the_first_hour():
    first_hour = np.r_[np.linspace(100, 101, 6), np.linspace(101, 100, 6)]  # IB 100-101
    rest = np.r_[np.full(3, 100.4), [100.1, 99.8, 100.1], np.full(57, 100.2)]
    s = IbFailure(AuctionConfig(entry_start=time(10, 30), min_risk_atr=0.1, min_rr=0.5))
    f = s.prepare(bars(DAY, np.r_[first_hour, rest]), None, None)
    hits = f.index[f["setup"]]
    assert len(hits) == 1 and hits[0] == DAY + pd.Timedelta("10h55min")


def test_auction_strategies_are_registered_and_validated():
    names = intraday_registry()
    assert {"va_rotation", "va_rejection", "ib_failure"} <= set(names)
    with pytest.raises(ConfigError):
        AuctionConfig(min_risk_atr=2, max_risk_atr=1)
    with pytest.raises(ConfigError):
        AuctionConfig(entry_end=time(16, 0))


def test_a_setup_before_the_entry_window_does_not_use_up_the_day():
    # the same rejection at 09:45 (before 10:00) and again at 10:50
    closes = np.r_[[100.0, 100.0, 99.3, 98.9, 99.2], np.full(9, 99.6), [99.3, 98.9, 99.2],
                   np.full(61, 99.6)]  # fmt: skip
    f = VaRejection().prepare(with_today(bars(DAY, closes)), None, None)
    hits = f.index[f["setup"]]
    assert len(hits) == 1 and hits[0] == DAY + pd.Timedelta("10h50min")
