# Author: Kai0421
"""Council scorecard: progress over time, plain English, no orders."""

import pandas as pd
import pytest

from bot import ricks_scorecard as sc


def trades(rows):
    """rows: (exit_date, pnl, r, version) -> a closed-trades frame."""
    return pd.DataFrame(
        [
            {
                "id": f"t{i}",
                "status": "closed",
                "exit_date": pd.Timestamp(d),
                "pnl": pnl,
                "r": r,
                "rules_version": v,
                "entry_price": 100.0,
                "shares": 10,
            }
            for i, (d, pnl, r, v) in enumerate(rows)
        ]
    )


def equity(values, spy=None):
    days = pd.bdate_range("2026-10-12", periods=len(values))
    return pd.DataFrame(
        {
            "day": days,
            "cash": values,
            "equity": values,
            "open": 0,
            "spy": spy or [100.0 + i for i in range(len(values))],
        }
    )


NO_RAISES = pd.DataFrame(columns=["id", "trade_id"])
ROWS = [
    ("2026-10-13", 100.0, 1.0, "v1"),
    ("2026-10-14", -50.0, -0.5, "v1"),
    ("2026-10-20", 30.0, 0.3, "v1"),
    ("2026-10-21", 70.0, 0.7, "v2"),
]


def test_wilson_interval_matches_known_values_and_handles_zero():
    lo, hi = sc.wilson_interval(8, 10)
    assert lo == pytest.approx(0.490, abs=0.01) and hi == pytest.approx(0.943, abs=0.01)
    assert sc.wilson_interval(0, 0) is None
    lo0, hi0 = sc.wilson_interval(0, 5)
    assert lo0 == 0.0 and 0 < hi0 < 0.6


def test_weekly_groups_by_the_week_the_trade_finished():
    w = sc.weekly(trades(ROWS))
    assert [b["week"] for b in w] == [pd.Timestamp("2026-10-12"), pd.Timestamp("2026-10-19")]
    assert w[0]["finished"] == 2 and w[0]["wins"] == 1 and w[0]["pnl"] == 50.0
    assert w[1]["finished"] == 2 and w[1]["win_rate"] == 1.0


def test_rolling_window_is_nan_until_enough_trades():
    many = trades(
        [(f"2026-10-{13 + i % 5:02d}", 1.0 if i % 2 else -1.0, 0.1, "v1") for i in range(25)]
    )
    r = sc.rolling(many, window=20)
    assert r["win_rate"].iloc[:19].isna().all() and r["win_rate"].iloc[19:].notna().all()
    assert sc.rolling(trades(ROWS))["win_rate"].isna().all()  # only 4 trades


def test_max_drawdown_is_peak_to_trough():
    dd = sc.max_drawdown(equity([100.0, 110.0, 99.0, 105.0]))
    assert dd == pytest.approx(0.10)
    assert sc.max_drawdown(equity([])) == 0.0


def test_versions_are_compared_separately():
    v = sc.by_version(trades(ROWS))
    assert v["v1"]["finished"] == 3 and v["v2"]["finished"] == 1


def test_build_and_verdict_against_the_hundred_trade_bar():
    card = sc.build(trades(ROWS), equity([100_000.0, 100_500.0, 100_200.0]), NO_RAISES)
    assert card["cumulative"]["finished"] == 4
    assert card["profit"] == pytest.approx(200.0) and card["stops_raised"] == 0
    assert sc.verdict(card) == "4 of 100 trades: too early to judge. Keep collecting."
    empty = sc.build(pd.DataFrame(), equity([]), NO_RAISES)
    assert "nothing to judge" in sc.verdict(empty)
    big = sc.build(trades([("2026-10-13", 1.0, 0.1, "v1")] * 100), equity([100_000.0]), NO_RAISES)
    assert "lower bound" in sc.verdict(big)


def test_stop_raises_are_counted():
    raises = pd.DataFrame({"id": ["a", "b"], "trade_id": ["t0", "t0"]})
    assert sc.build(trades(ROWS), equity([100_000.0]), raises)["stops_raised"] == 2


ABL = {
    "by_rick": {
        "crowd": {"n": 12, "mean_x1_bps": 3.0, "mean_x5_bps": 20.0, "hit5": 0.58},
        "tape": {"n": 0, "mean_x1_bps": None, "mean_x5_bps": None, "hit5": None},
        "skeptic": {"n": 30, "mean_x1_bps": -1.0, "mean_x5_bps": -8.0, "hit5": 0.47},
    },
    "buys": {"n": 5, "mean_x1_bps": 1.0, "mean_x5_bps": 15.0, "hit5": 0.6},
}


def test_weekly_message_is_plain_english_and_labelled_practice():
    card = sc.build(trades(ROWS), equity([100_000.0, 100_500.0, 100_200.0]), NO_RAISES, ABL)
    title, text = sc.weekly_message(card, pd.Timestamp("2026-10-12"))
    assert "week of 12 Oct" in title
    assert "2 pretend trades finished, 1 won" in text
    assert "Crowd Rick" in text and "Skeptic Rick" in text and "Rick C127" not in text
    assert "4 of 100 trades: too early" in text
    assert "Rules version in use: v2." in text
    assert "do not learn by themselves" in text and "no real order" in text.lower()
    for bad in ("nan", "None", "R multiple", "expectancy", "Wilson", "_"):
        assert bad not in text, bad
    quiet = sc.weekly_message(card, pd.Timestamp("2026-11-02"))[1]
    assert "No pretend trades finished this week." in quiet


def test_weekly_message_with_nothing_yet_does_not_crash():
    card = sc.build(pd.DataFrame(), equity([]), NO_RAISES)
    title, text = sc.weekly_message(card, pd.Timestamp("2026-10-12"))
    assert "No pretend trades finished" in text and "Each Rick on his own: not enough" in text


def test_html_report_is_self_contained_themed_and_mobile_ready():
    eq = equity([100_000.0, 100_400.0, 100_100.0, 100_700.0])
    card = sc.build(trades(ROWS), eq, NO_RAISES, ABL)
    page = sc.html_report(card, "generated for the test")
    assert page.startswith("<!doctype html>") and "viewport" in page
    assert "prefers-color-scheme:dark" in page and "<svg" in page
    assert "http://" not in page and "https://" not in page and "<script" not in page
    assert "Council" in page and "SPY" in page and "Crowd Rick said yes" in page
    assert "4 of 100 trades" in page and "Needs 20 finished trades first." in page
    assert "Table view" in page and "docs/22-council-changelog.md" in page


def test_html_report_handles_an_empty_ledger():
    page = sc.html_report(sc.build(pd.DataFrame(), equity([]), NO_RAISES))
    assert "No days yet." in page and "No finished trades yet" in page
