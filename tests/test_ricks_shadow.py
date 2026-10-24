# Author: Kai0421
"""Council shadow log: risk inputs, daily convening, outcome labels, demotion rules."""

import numpy as np
import pandas as pd
import pytest

from bot import ricks_shadow as rs
from bot.sentiment import Snapshot


def bars(n=260, start=100.0, drift=0.001, volume=2_000_000, last_jump=0.0, seed=1):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + drift + rng.normal(0, 0.004, n))
    close[-1] = close[-2] * (1 + last_jump)
    idx = pd.bdate_range(end="2026-10-09", periods=n)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": float(volume),
        },
        index=idx,
    )


def snap(symbol="AAA", score=80.0, volume_score=70.0, post_net=0.6, tagged=10):
    return Snapshot(
        ts="2026-10-09T20:30:00+00:00",
        symbol=symbol,
        price=100.0,
        score=score,
        volume_score=volume_score,
        post_net=post_net,
        post_tagged=tagged,
    )


# ------------------------------------------------------------------ risk inputs


def test_risk_context_reads_market_move_and_liquidity():
    spy = bars(drift=0.001, seed=2)
    ctx = rs.risk_context(bars(last_jump=0.0), spy, sector_open=1, sector_cap=3)
    assert ctx.spy_above_sma200 is True
    assert ctx.avg_dollar_volume and ctx.avg_dollar_volume > 20e6
    assert ctx.sector_open == 1 and ctx.sector_cap == 3
    assert ctx.earnings_in_days is None  # no earnings source yet: Skeptic penalises it


def test_risk_context_flags_big_move_in_atr_units():
    big = rs.risk_context(bars(last_jump=0.08), bars(seed=2))
    calm = rs.risk_context(bars(last_jump=0.0), bars(seed=2))
    assert big.move_atr > 2.5 > calm.move_atr


def test_risk_context_market_below_trend():
    spy = bars(drift=-0.002, seed=3)
    assert rs.risk_context(bars(), spy).spy_above_sma200 is False


def test_risk_context_unknown_when_history_is_short():
    ctx = rs.risk_context(bars(n=50), bars(n=50))
    assert ctx.spy_above_sma200 is None


# ------------------------------------------------------------------- convening


def test_convene_day_makes_one_row_per_symbol_with_votes():
    data = {"AAA": bars(drift=0.002, seed=4), "BBB": bars(drift=-0.002, seed=5)}
    snaps = {"AAA": snap("AAA"), "BBB": snap("BBB", score=20, post_net=-0.5)}
    rows = rs.convene_day(
        pd.Timestamp("2026-10-09"), snaps, data, bars(seed=2), sectors={"AAA": "t", "BBB": "t"}
    )
    assert {r["symbol"] for r in rows} == {"AAA", "BBB"}
    r = next(r for r in rows if r["symbol"] == "AAA")
    for key in ("crowd", "tape", "skeptic", "mean", "buy", "reason", "close"):
        assert key in r


def test_convene_day_skips_symbols_without_bars_or_snapshot_still_votes_abstain():
    data = {"AAA": bars(seed=4)}
    rows = rs.convene_day(
        pd.Timestamp("2026-10-09"), {}, data, bars(seed=2), sectors={"AAA": "t", "ZZZ": "t"}
    )
    assert [r["symbol"] for r in rows] == ["AAA"]  # no bars for ZZZ: nothing to log
    assert rows[0]["crowd"] == 0.0 and rows[0]["buy"] is False  # no data cannot buy


def test_sector_counts_come_from_held_symbols():
    data = {"AAA": bars(seed=4)}
    rows = rs.convene_day(
        pd.Timestamp("2026-10-09"),
        {"AAA": snap()},
        data,
        bars(seed=2),
        sectors={"AAA": "t", "HELD1": "t", "HELD2": "t", "HELD3": "t"},
        held={"HELD1", "HELD2", "HELD3"},
        sector_cap=3,
    )
    assert "sector full" in rows[0]["reason"]


# ---------------------------------------------------------------------- labels


def log_rows(days, symbol="AAA", buy=True):
    return pd.DataFrame(
        [
            {"day": d, "symbol": symbol, "buy": buy, "crowd": 0.5, "tape": 0.4, "skeptic": 0.3}
            for d in days
        ]
    )


def test_label_uses_next_open_and_later_closes_and_waits_for_the_future():
    b = bars(n=30, seed=6)
    day = b.index[-8]
    log = log_rows([day, b.index[-1]])
    out = rs.label(log, {"AAA": b})
    first, last = out.iloc[0], out.iloc[1]
    entry = b["open"].iloc[-7]
    assert first["ret1"] == pytest.approx(b["close"].iloc[-7] / entry - 1)
    assert first["ret5"] == pytest.approx(b["close"].iloc[-3] / entry - 1)
    assert pd.isna(last["ret1"]) and pd.isna(last["ret5"])  # nothing after the last bar yet


def test_label_is_idempotent():
    b = bars(n=30, seed=6)
    log = log_rows([b.index[-8]])
    once = rs.label(log, {"AAA": b})
    twice = rs.label(once, {"AAA": b})
    pd.testing.assert_frame_equal(once, twice)


def test_excess_is_against_the_average_of_that_days_rows():
    df = pd.DataFrame(
        {
            "day": ["d1", "d1", "d2"],
            "ret1": [0.02, 0.0, 0.01],
            "ret5": [0.05, 0.01, 0.0],
        }
    )
    out = rs.add_excess(df)
    assert out["x1"].tolist() == pytest.approx([0.01, -0.01, 0.0])
    assert out["x5"].tolist() == pytest.approx([0.02, -0.02, 0.0])


# ------------------------------------------------------------ summary / ablation


def test_summary_compares_buys_to_all_and_reports_each_rick():
    df = pd.DataFrame(
        {
            "day": ["d1"] * 4,
            "buy": [True, True, False, False],
            "crowd": [0.8, 0.6, -0.2, 0.1],
            "tape": [0.5, 0.4, 0.0, -0.5],
            "skeptic": [0.5, 0.5, 0.1, 0.2],
            "ret1": [0.02, 0.01, -0.01, -0.02],
            "ret5": [0.04, 0.02, -0.02, -0.04],
        }
    )
    out = rs.summary(rs.add_excess(df))
    assert out["all"]["n"] == 4
    assert out["buys"]["n"] == 2
    assert out["buys"]["mean_x5_bps"] > 0
    assert set(out["by_rick"]) == {"crowd", "tape", "skeptic"}


def test_summary_handles_no_buys():
    df = pd.DataFrame(
        {
            "day": ["d1"],
            "buy": [False],
            "crowd": [0.0],
            "tape": [0.0],
            "skeptic": [0.0],
            "ret1": [0.01],
            "ret5": [0.01],
        }
    )
    assert rs.summary(rs.add_excess(df))["buys"]["n"] == 0


# -------------------------------------------------------------------- demotion


def test_demotion_needs_enough_trades_for_the_average_test():
    assert not rs.demotion([-1.0] * 4).demote  # too few for the average, streak below 5
    assert rs.demotion([-0.2] * 20).demote  # 20 trades, negative mean


def test_demotion_on_losing_streak():
    r = rs.demotion([1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0, -1.0])
    assert r.demote and "streak" in r.reason


def test_demotion_on_drawdown_in_r():
    results = [1.0, 1.0] + [-1.0, 0.5] * 4 + [-1.0, -1.0, -1.0, 0.5]
    r = rs.demotion(results, rs.TrialRules(max_drawdown_r=3.0, max_losing_streak=99))
    assert r.demote and "drawdown" in r.reason


def test_no_demotion_when_results_are_fine():
    assert not rs.demotion([1.0, -1.0, 0.5, 1.0, -1.0, 1.0] * 4).demote


def test_demotion_on_stale_data_is_a_separate_check():
    assert rs.demotion([1.0], data_fresh=False).demote


def test_tape_and_skeptic_alone_cannot_buy_without_the_crowd():
    from bot.ricks import convene

    b = bars(drift=0.003, seed=7)
    ctx = rs.risk_context(b, bars(seed=2), earnings_in_days=30)
    with_crowd = convene(snap(score=85, volume_score=80, post_net=0.8), b, ctx)
    without = convene(None, b, ctx)
    assert with_crowd.buy
    assert not without.buy and without.reason == "crowd is not for"
