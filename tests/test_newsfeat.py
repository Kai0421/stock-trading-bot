# Author: Kai0421
"""News features for the council shadow log: classification, windowing, log-only columns."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from bot import newsfeat as nf
from bot import ricks_shadow as rs
from bot.sentiment import Snapshot


def bars(n=260, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + 0.001 + rng.normal(0, 0.004, n))
    idx = pd.bdate_range(end="2026-10-09", periods=n)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": 2_000_000.0,
        },
        index=idx,
    )


def snap():
    return Snapshot(
        ts="2026-10-09T20:30:00+00:00",
        symbol="AAA",
        price=100.0,
        score=80.0,
        volume_score=70.0,
        post_net=0.6,
        post_tagged=10,
    )


START = datetime(2026, 10, 8, 19, 50, tzinfo=UTC)
END = datetime(2026, 10, 9, 19, 50, tzinfo=UTC)


def item(headline, symbols=("AAA",), created="2026-10-09T12:00:00Z"):
    return {"headline": headline, "symbols": list(symbols), "created_at": created}


def test_classify_flags_risk_and_earnings_words():
    assert nf.classify("Analyst downgrades AAA after probe") == (True, False)
    assert nf.classify("AAA reports Q3 results after the bell") == (False, True)
    assert nf.classify("AAA unveils new product line") == (False, False)
    assert nf.classify("Stocks tumble as AAA warns on guidance") == (True, False)


def test_features_count_per_symbol_with_zeros_for_quiet_stocks():
    out = nf.features(
        [item("AAA downgraded"), item("AAA earnings preview"), item("BBB launches app", ("BBB",))],
        ["AAA", "BBB", "CCC"],
        START,
        END,
    )
    assert out["AAA"]["news_n"] == 2 and out["AAA"]["news_risk_n"] == 1
    assert out["AAA"]["news_earn_n"] == 1 and "downgraded" in out["AAA"]["news_risk_heads"]
    assert out["BBB"]["news_n"] == 1 and out["BBB"]["news_risk_n"] == 0
    assert out["CCC"]["news_n"] == 0  # no news is zero, not missing


def test_features_ignore_outside_window_roundups_and_unknown_symbols():
    out = nf.features(
        [
            item("AAA downgraded", created="2026-10-09T19:50:00Z"),  # window end is exclusive
            item("AAA downgraded", created="2026-10-08T19:49:00Z"),  # before the window
            item("Market roundup", ("AAA", "B", "C", "D", "E", "F")),  # too many tags
            item("ZZZ downgraded", ("ZZZ",)),  # not in the universe
        ],
        ["AAA"],
        START,
        END,
    )
    assert out["AAA"]["news_n"] == 0 and "ZZZ" not in out


def test_news_columns_are_logged_but_never_change_the_votes():
    sectors = {"AAA": "tech"}
    b = {"AAA": bars()}
    day = b["AAA"].index[-1]
    plain = rs.convene_day(day, {"AAA": snap()}, b, bars(seed=2), sectors)[0]
    news = nf.features([item("AAA downgraded")], ["AAA"], START, END)
    withnews = rs.convene_day(day, {"AAA": snap()}, b, bars(seed=2), sectors, news=news)[0]
    assert withnews["news_risk_n"] == 1 and "news_n" not in plain
    for key in ("crowd", "tape", "skeptic", "mean", "buy", "reason"):
        assert withnews[key] == plain[key]


def test_news_summary_compares_risk_headline_rows_with_the_rest():
    df = pd.DataFrame(
        {
            "day": [pd.Timestamp("2026-10-01")] * 4,
            "symbol": list("ABCD"),
            "ret1": [-0.02, 0.01, 0.0, 0.01],
            "ret5": [-0.04, 0.02, 0.0, 0.02],
            "news_n": [2, 1, 0, 3],
            "news_risk_n": [1, 0, 0, 0],
            "news_earn_n": [0, 0, 0, 1],
        }
    )
    s = rs.news_summary(rs.add_excess(df))
    assert s["risk_headline"]["n"] == 1 and s["risk_headline"]["mean_x5_bps"] < 0
    assert s["no_news"]["n"] == 1 and s["earnings_headline"]["n"] == 1
    assert rs.news_summary(pd.DataFrame({"day": [], "ret5": []})) == {}


def test_digest_lists_buys_with_votes_and_news_and_says_no_orders():
    rows = [
        {
            "day": pd.Timestamp("2026-10-09"),
            "symbol": "AAA",
            "crowd": 0.8,
            "tape": 0.5,
            "skeptic": 0.4,
            "mean": 0.57,
            "buy": True,
            "reason": "ok",
            "news_n": 3,
            "news_risk_n": 1,
        },
        {
            "day": pd.Timestamp("2026-10-09"),
            "symbol": "BBB",
            "crowd": -0.2,
            "tape": 0.1,
            "skeptic": 0.3,
            "mean": 0.07,
            "buy": False,
            "reason": "crowd is not for | x",
        },
    ]
    title, text = rs.digest(rows)
    assert "Fri 09 Oct" in title and "practice run" in title
    assert "would have bought 1" in text and "AAA" in text and "would buy" in text
    assert (
        "likes it (+0.8)" in text
        and "Crowd Rick" in text
        and "3 headlines, 1 with a warning word" in text
    )
    assert "No orders were placed" in text and "BBB" not in text.split("warning-style")[0]
    _, none = rs.digest([dict(rows[1])])
    assert "would not have bought any" in none and "the crowd isn't behind it" in none
