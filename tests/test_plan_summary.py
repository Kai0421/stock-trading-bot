# Author: Kai0421
from types import SimpleNamespace as N

from bot import plan_summary as ps

FEATURES = '{"rsi_min": 22.6, "momentum": 0.1053}'


def held(symbol, shares, stop, exit_=None, days=0):
    return N(symbol=symbol, shares=shares, stop=stop, days_held=days, pending_exit=exit_)


def test_before_the_morning_plan_it_says_buys_are_not_decided_yet():
    title, text = ps.summary(
        "Mon 19 Oct",
        premarket_ran=False,
        plans=[],
        signals=[],
        positions=[held("BLK", 17, 1092.39)],
    )
    assert "Mon 19 Oct" in title
    assert "not made yet" in text and "13:30 UK" in text
    assert "Sell: nothing" in text and "BLK" in text and "1,092.39" in text


def test_after_the_plan_buys_come_with_reasons_and_sales_with_reasons():
    plans = [N(symbol="GILD", win_prob=0.71)]
    signals = [{"symbol": "GILD", "win_prob": 0.71, "features": FEATURES, "reason": "planned"}]
    positions = [held("TMO", 26, 657.45, ("time_stop", 26), days=7)]
    _, text = ps.summary("Tue 20 Oct", True, plans, signals, positions)
    assert "Buy:" in text and "GILD" in text and "Uptrend, sharp dip" in text
    assert (
        "Sell (at today's open)" in text
        and "TMO" in text
        and "held the maximum number of days" in text
    )
    for bad in ("time_stop", "None", "nan"):
        assert bad not in text


def test_plan_made_but_nothing_to_buy_or_sell():
    _, text = ps.summary("Tue 20 Oct", True, [], [], [held("BLK", 17, 1092.39)])
    assert "Buy: nothing planned today" in text and "Sell: nothing" in text


def test_summary_is_labelled_paper_and_plain():
    _, text = ps.summary("Mon 19 Oct", False, [], [], [])
    assert "paper" in text.lower() and "No open positions" in text
