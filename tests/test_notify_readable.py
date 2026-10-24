# Author: Kai0421
"""Alert wording: internal reason codes and job names become plain English (text only)."""

import pytest

from bot.notify import job_name, plain_reason, sentence


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("max per sector reached for Information Technology", "Information Technology sector"),
        ("max open positions (6) reached", "maximum of 6 positions"),
        ("not enough tradable cash", "spare cash"),
        ("kill switch: daily loss -4.00%", "safety stop"),
        ("negative cash (margin used): buys blocked, manual review", "borrowing"),
        ("margin/shorting lock not verified: buys blocked", "cash-only"),
        ("account fetch failed: no orders", "couldn't read the account"),
        ("AAPL: duplicate in batch", "AAPL was listed twice"),
        ("AAPL: unknown sector", "doesn't know which sector AAPL"),
        ("AAPL: invalid stop 95.0", "stop-loss price for AAPL ($95.00)"),
        ("TQQQ: leveraged/inverse/volatility product", "leveraged"),
        ("XYZ: not in the whitelist", "approved list"),
        ("win rate 57.8% below target 60%", "chance of winning (57.8%)"),
        ("win rate below target 60%", "below the required 60%"),
        ("expectancy -0.002R is not positive", "loses a little money"),
        ("95% lower bound of mean R -0.012R is not positive", "aren't strong enough"),
        ("95% lower bound 57.1% below floor 60%", "57.1%"),
        ("95% lower bound below floor 62%", "worst case win rate must be at least 62%"),
        ("only 40 matching trades (need 100)", "only 40 similar past trades"),
        ("7-day usage 85% >= 80%", "weekly usage (85%)"),
        ("5-hour usage 93% >= 90%", "5-hour usage (93%)"),
    ],
)
def test_internal_reasons_become_plain_sentences(raw, expect):
    out = plain_reason(raw)
    assert expect in out and out != raw
    for code in ("max_per", "kill switch:", "R is", "R "):
        assert code not in out or code == "R "  # no internal codes left


def test_unknown_reasons_pass_through_unchanged():
    assert plain_reason("something new") == "something new"


def test_job_names_are_friendly_and_unknown_ones_are_tidied():
    assert job_name("premarket") == "morning check"
    assert job_name("close_buy") == "buy before the close"
    assert job_name("brand_new_job") == "brand new job"


def test_sentence_capitalises_and_ends_with_a_full_stop():
    assert sentence("the bot already holds its maximum") == "The bot already holds its maximum."
    assert sentence("Already done.") == "Already done."
    assert sentence("") == ""


def test_plan_table_lists_planned_then_skipped_with_win_chance():
    from types import SimpleNamespace

    from bot.notify import plan_table

    plans = [
        SimpleNamespace(symbol="MDT", win_prob=0.70),
        SimpleNamespace(symbol="AAA", win_prob=0.65),
    ]
    signals = [
        {"symbol": "ZZZ", "win_prob": 0.55},
        {"symbol": "AAA", "win_prob": 0.65},
        {"symbol": "MDT", "win_prob": 0.70},
    ]
    out = plan_table(signals, plans)
    rows = out.strip("`\n").splitlines()
    assert rows[0].split() == ["Stock", "Win", "chance", "Decision", "Reason"]
    assert rows[2].split()[:2] == ["MDT", "70%"] and "Buy (plan #1)" in rows[2]
    assert "Buy (plan #2)" in rows[3]
    assert rows[4].split()[:2] == ["ZZZ", "55%"] and "Skipped" in rows[4]
    assert out.startswith("```") and out.endswith("```")


def test_plan_table_caps_long_lists():
    from bot.notify import plan_table

    signals = [{"symbol": f"S{i:02d}", "win_prob": 0.5} for i in range(14)]
    out = plan_table(signals, [])
    assert "... and 4 more skipped" in out


FEATURES = '{"strategy": "trend_pullback", "rsi_min": 22.6, "momentum": 0.1053, "atr": 2.0}'


def test_buy_reason_is_plain_english_and_short():
    from bot.notify import buy_reason

    text = buy_reason({"features": FEATURES})
    assert text == "Uptrend, sharp dip, up 11% in 6 mo"
    assert buy_reason({"features": '{"rsi_min": 41.0, "momentum": 0.2}'}) == (
        "Uptrend, dip, up 20% in 6 mo"
    )
    assert buy_reason({"features": '{"rsi_min": 60.0}'}) == "Uptrend, pullback to its average"
    for bad in ("RSI", "nan", "None", "trend_pullback"):
        assert bad not in text
    assert buy_reason({}) == "Met the buy rules"
    assert buy_reason({"features": "not json"}) == "Met the buy rules"
    assert len(buy_reason({"features": FEATURES})) <= 36


def test_plan_table_shows_a_reason_for_buys_and_for_skips():
    from types import SimpleNamespace

    from bot.notify import plan_table

    plans = [SimpleNamespace(symbol="MDT", win_prob=0.70)]
    signals = [
        {"symbol": "MDT", "win_prob": 0.70, "features": FEATURES, "reason": "planned"},
        {"symbol": "ZZZ", "win_prob": 0.55, "reason": "max per sector reached for Financials"},
        {"symbol": "YYY", "win_prob": 0.52, "reason": "win chance 52% below the 60% bar"},
    ]
    rows = plan_table(signals, plans).strip("`\n").splitlines()
    assert "Uptrend, sharp dip" in rows[2]
    zzz = next(r for r in rows if r.startswith("ZZZ"))
    assert "Skipped (sector full)" in zzz and "sector already holds the limit" in zzz
    yyy = next(r for r in rows if r.startswith("YYY"))
    assert yyy.rstrip().endswith("...") or "below" in yyy or "win chance" in yyy.lower()
    assert max(len(r) for r in rows) <= 84  # still readable in a Slack code block


def test_sale_table_lists_each_planned_sale_with_a_plain_reason():
    from types import SimpleNamespace

    from bot.notify import sale_table

    held = [
        SimpleNamespace(symbol="TMO", shares=26, days_held=7, pending_exit=("time_stop", 26)),
        SimpleNamespace(symbol="LLY", shares=13, days_held=3, pending_exit=("trend_break", 13)),
        SimpleNamespace(symbol="BLK", shares=17, days_held=2, pending_exit=None),
        SimpleNamespace(symbol="MDT", shares=100, days_held=4, pending_exit=("partial_profit", 40)),
    ]
    out = sale_table(held)
    rows = out.strip("`\n").splitlines()
    assert rows[0].split() == ["Stock", "Shares", "Reason"]
    assert any(r.startswith("TMO") and "26" in r and "held the maximum" in r for r in rows)
    assert any(r.startswith("LLY") and "uptrend broke" in r for r in rows)
    assert any(r.startswith("MDT") and "40" in r and "some profit" in r for r in rows)
    assert not any(r.startswith("BLK") for r in rows)  # nothing planned for BLK
    for bad in ("time_stop", "trend_break", "None"):
        assert bad not in out


def test_sale_table_is_empty_when_nothing_is_planned():
    from types import SimpleNamespace

    from bot.notify import sale_table

    assert sale_table([SimpleNamespace(symbol="A", shares=1, days_held=1, pending_exit=None)]) == ""
