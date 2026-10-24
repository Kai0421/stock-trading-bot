# Author: Kai0421
"""Council paper ledger: pretend buys and sells, no broker, no orders."""

import numpy as np
import pandas as pd
import pytest

from bot import ricks_paper as rp

DAYS = pd.bdate_range("2026-01-05", periods=40)  # Mon 5 Jan onwards


def bars(path: dict[int, tuple[float, float, float, float]] | None = None, base=100.0, half=1.0):
    """Flat bars at ``base`` (range 2), with per-day (open, high, low, close) overrides."""
    rows = []
    for i in range(len(DAYS)):
        rows.append((path or {}).get(i, (base, base + half, base - half, base)))
    df = pd.DataFrame(rows, index=DAYS, columns=["open", "high", "low", "close"])
    df["volume"] = 1_000_000.0
    return df


def log_rows(day_idx: int, symbols=("AAA",), crowd=0.5, tape=0.6, skeptic=0.3, buy=True):
    rows = []
    for k, s in enumerate(symbols):
        rows.append(
            {
                "day": DAYS[day_idx],
                "symbol": s,
                "crowd": crowd,
                "tape": tape,
                "skeptic": skeptic,
                "mean": 0.5 - 0.01 * k,
                "buy": buy,
            }
        )
    return pd.DataFrame(rows)


def run(log, bs, sectors=None, through=None, trail=None):
    sectors = sectors or {s: "Tech" for s in bs}
    return rp.replay(log, bs, sectors, through=through, trail=trail)


def spy():
    return bars()


def one(res):
    assert len(res.trades) == 1
    return res.trades.iloc[0]


def test_buy_at_next_open_with_two_atr_stop_and_risk_sizing():
    # enough history for ATR 14: signal on day 20, enters at the day-21 open
    aaa = bars({21: (102.0, 103.0, 101.0, 102.5)})
    res = run(log_rows(20), {"AAA": aaa, "SPY": spy()}, through=DAYS[22])
    t = one(res)
    assert t["entry_date"] == DAYS[21] and t["entry_price"] == 102.0
    atr = 2.0  # every flat day has range 2
    assert t["initial_stop"] == pytest.approx(102.0 - 2 * atr, abs=0.3)
    risk = (t["entry_price"] - t["initial_stop"]) * t["shares"]
    assert risk <= 0.01 * 100_000 + 1  # at most 1% of equity at risk
    assert t["shares"] > 0 and t["status"] == "open"


def test_stop_hit_fills_at_stop_and_gap_down_fills_at_open():
    aaa = bars({21: (100.0, 101.0, 99.0, 100.0), 22: (100.0, 100.5, 95.0, 96.0)})
    t = one(run(log_rows(20), {"AAA": aaa, "SPY": spy()}))
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(t["stop"])
    assert t["pnl"] < 0 and t["status"] == "closed"
    gap = bars({21: (100.0, 101.0, 99.0, 100.0), 22: (90.0, 91.0, 89.0, 90.0)})
    g = one(run(log_rows(20), {"AAA": gap, "SPY": spy()}))
    assert g["exit_price"] == 90.0  # gapped below the stop: sold at the open


def test_stop_wins_a_same_day_tie_with_the_time_exit():
    path = {i: (100.0, 101.0, 99.0, 100.0) for i in range(21, 26)}
    path[25] = (100.0, 101.0, 90.0, 110.0)  # day 5 of the hold: stopped out, then rallied
    t = one(run(log_rows(20), {"AAA": bars(path), "SPY": spy()}))
    assert t["exit_reason"] == "stop"


def test_time_exit_at_the_close_of_the_fifth_session():
    path = {21: (100.0, 101.0, 99.0, 100.0), 25: (100.0, 112.0, 99.0, 111.0)}
    t = one(run(log_rows(20), {"AAA": bars(path), "SPY": spy()}))
    assert t["exit_reason"] == "time" and t["exit_date"] == DAYS[25]
    assert t["exit_price"] == 111.0 and t["pnl"] > 0


def test_council_turning_against_sells_at_the_next_open():
    log = pd.concat([log_rows(20), log_rows(22, tape=-0.8, buy=False)])  # a veto on day 22
    path = {23: (104.0, 105.0, 103.0, 104.5)}
    t = one(run(log, {"AAA": bars(path), "SPY": spy()}))
    assert t["exit_reason"] == "council" and t["exit_date"] == DAYS[23]
    assert t["exit_price"] == 104.0
    assert "Rick C127" in t["exit_note"]


def test_two_ricks_against_also_sells_but_one_does_not():
    one_against = log_rows(22, crowd=-0.4, buy=False)
    t = one(run(pd.concat([log_rows(20), one_against]), {"AAA": bars(), "SPY": spy()}))
    assert t["exit_reason"] == "time"
    two = log_rows(22, crowd=-0.4, skeptic=-0.4, buy=False)
    t2 = one(run(pd.concat([log_rows(20), two]), {"AAA": bars(), "SPY": spy()}))
    assert t2["exit_reason"] == "council"


def test_sector_cap_and_open_position_cap():
    syms = ("A1", "A2", "A3")
    bs = {s: bars() for s in syms} | {"SPY": spy()}
    res = run(log_rows(20, syms), bs, sectors={s: "Tech" for s in syms} | {"SPY": "ETF"})
    assert list(res.trades["symbol"]) == ["A1", "A2"]  # max 2 per sector, best mean first
    many = tuple(f"S{i}" for i in range(9))
    bs2 = {s: bars(half=10.0) for s in many} | {"SPY": spy()}
    sec = {s: f"Sector{i}" for i, s in enumerate(many)} | {"SPY": "ETF"}
    assert len(run(log_rows(20, many), bs2, sectors=sec).trades) == 6


def test_never_spends_more_than_the_cash_it_has():
    many = tuple(f"S{i}" for i in range(6))
    bs = {s: bars(base=900.0) for s in many} | {"SPY": spy()}
    sec = {s: f"Sector{i}" for i, s in enumerate(many)} | {"SPY": "ETF"}
    res = run(log_rows(20, many), bs, sectors=sec)
    spent = (res.trades["entry_price"] * res.trades["shares"]).sum()
    assert spent <= 100_000 * 0.98 + 1e-6
    assert (res.equity["cash"] >= -1e-6).all()


def test_replay_is_idempotent_and_sent_flags_survive():
    path = {21: (100.0, 101.0, 99.0, 100.0), 25: (100.0, 112.0, 99.0, 111.0)}
    args = (log_rows(20), {"AAA": bars(path), "SPY": spy()})
    first = run(*args)
    again = run(*args)
    pd.testing.assert_frame_equal(first.trades, again.trades)
    old = first.trades.copy()
    old["buy_sent"] = 1
    merged = rp.merge_sent(again.trades, old)
    assert merged["buy_sent"].tolist() == [1] and merged["sell_sent"].tolist() == [0]


def test_through_ignores_bars_after_that_day():
    path = {21: (100.0, 101.0, 99.0, 100.0), 25: (100.0, 112.0, 99.0, 111.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[23])
    t = one(res)
    assert t["status"] == "open"
    assert res.equity["day"].max() == DAYS[23]


def test_summary_counts_and_enough_data_line():
    path = {21: (100.0, 101.0, 99.0, 100.0), 25: (100.0, 112.0, 99.0, 111.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()})
    s = rp.summary(res.trades, res.equity)
    assert s["closed"] == 1 and s["wins"] == 1 and s["open"] == 0
    assert s["total_pnl"] > 0 and s["equity"] > 100_000
    assert "far too early" in rp.verdict_line(s)
    assert "No pretend trades yet" in rp.verdict_line(rp.summary(res.trades.iloc[0:0], res.equity))


def _plain(text: str):
    for bad in ("max_per_sector", "Traceback", "NaN", "exit_reason", " R ", "expectancy", "OPG"):
        assert bad not in text


def test_messages_are_plain_english_and_labelled_practice():
    path = {21: (100.0, 101.0, 99.0, 100.0), 25: (100.0, 112.0, 99.0, 111.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()})
    t = res.trades.iloc[0]
    s = rp.summary(res.trades, res.equity)
    title, text = rp.buy_message(t, s)
    assert "AAA" in title and "Practice" in title
    assert "pretend-bought" in text and "Crowd Rick" in text and "Rick C127" in text
    assert "no real order" in text.lower()
    _plain(text)
    title, text = rp.sell_message(t, s)
    assert "pretend-sold" in text and "5-day" in text and "no real order" in text.lower()
    assert "$" in text and "profit so far" in text
    _plain(text)
    stop = rp.sell_message({**t, "exit_reason": "stop"}, s)[1]
    assert "stop-loss" in stop
    flip = rp.sell_message(
        {**t, "exit_reason": "council", "exit_note": "Skeptic Rick vetoed it"}, s
    )
    assert "turned against" in flip[1] and "Skeptic Rick vetoed it" in flip[1]


def test_send_events_marks_flags_and_never_resends_or_raises():
    path = {21: (100.0, 101.0, 99.0, 100.0), 25: (100.0, 112.0, 99.0, 111.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()})
    sent = []

    def ok(title, text):
        sent.append(title)

    trades, n = rp.send_events(res.trades, rp.summary(res.trades, res.equity), ok)
    assert n == 2 and trades["buy_sent"].tolist() == [1] and trades["sell_sent"].tolist() == [1]
    trades, n = rp.send_events(trades, rp.summary(trades, res.equity), ok)
    assert n == 0 and len(sent) == 2  # nothing sent twice

    def boom(title, text):
        raise RuntimeError("slack down")

    trades2, n2 = rp.send_events(res.trades, rp.summary(res.trades, res.equity), boom)
    assert n2 == 0 and trades2["buy_sent"].tolist() == [0]  # retried on the next run


def test_open_trade_sends_only_the_buy_message():
    path = {21: (100.0, 101.0, 99.0, 100.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[22])
    trades, n = rp.send_events(res.trades, rp.summary(res.trades, res.equity), lambda a, b: None)
    assert n == 1 and trades["sell_sent"].tolist() == [0]


def test_daily_message_lists_open_positions_and_profit():
    path = {21: (100.0, 101.0, 99.0, 100.0), 22: (100.0, 103.0, 99.0, 102.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[22])
    s = rp.summary(res.trades, res.equity)
    title, text = rp.daily_message(DAYS[22], res.trades, s)
    assert "AAA" in text and "practice" in text.lower() and "$" in text
    _plain(text)
    assert not np.isnan(s["equity"])


def test_messages_survive_a_csv_round_trip_with_blank_fields(tmp_path):
    res = run(log_rows(20), {"AAA": bars(), "SPY": spy()}, through=DAYS[22])
    res.trades.assign(votes_other="").to_csv(tmp_path / "t.csv", index=False)
    t = pd.read_csv(tmp_path / "t.csv").iloc[0].to_dict()  # blank text comes back as NaN
    text = rp.buy_message(t, rp.summary(res.trades, res.equity))[1]
    assert "nan" not in text.lower()
    t2 = {**t, "status": "closed", "exit_reason": "council", "exit_note": float("nan"),
          "exit_price": 99.0, "pnl": -5.0}  # fmt: skip
    assert "nan" not in rp.sell_message(t2, rp.summary(res.trades, res.equity))[1].lower()


# ----------------------------------------------------------------- trailing stop


def test_trail_stop_only_rises_and_never_below_the_first_stop():
    assert rp.trail_stop(96.0, 96.0, 120.0, 2.0) == 116.0  # highest close 120 minus 2 x ATR
    assert rp.trail_stop(116.0, 96.0, 110.0, 2.0) == 116.0  # price fell back: stop stays
    assert rp.trail_stop(96.0, 96.0, 100.0, 2.0) == 96.0  # price did not rise: first stop
    assert rp.trail_stop(110.0, 96.0, 100.0, 2.0) == 110.0  # never down


def test_stop_ratchets_up_at_the_close_and_applies_the_next_session():
    path = {
        21: (100.0, 101.0, 99.0, 100.0),
        22: (100.0, 112.0, 99.5, 110.0),  # rally: close 110 -> stop 106 from day 23
        23: (110.0, 111.0, 105.0, 107.0),  # low 105 <= 106: stopped at 106 (> first stop 96)
    }
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, trail=True)
    t = one(res)
    assert t["initial_stop"] == pytest.approx(96.0, abs=0.2)
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(106.0, abs=0.2)
    assert t["pnl"] > 0  # the raised stop locked in a profit
    assert t["stops_raised"] >= 1
    assert (res.raises["new_stop"] > res.raises["old_stop"]).all()


def test_raise_takes_effect_next_session_not_the_same_day():
    # day 22 closes at 110 but its low (99.5) is above the old stop and below the new one
    path = {21: (100.0, 101.0, 99.0, 100.0), 22: (100.0, 112.0, 99.5, 110.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[22], trail=True)
    assert one(res)["status"] == "open"  # not stopped on the day it was raised


def test_gap_below_a_raised_stop_fills_at_the_open():
    path = {
        21: (100.0, 101.0, 99.0, 100.0),
        22: (100.0, 112.0, 99.5, 110.0),
        23: (101.0, 102.0, 100.0, 101.5),  # opens below the raised stop of about 106
    }
    t = one(run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, trail=True))
    assert t["exit_reason"] == "stop" and t["exit_price"] == 101.0


def test_initial_stop_unchanged_when_the_price_only_falls():
    path = {21: (100.0, 100.5, 98.5, 99.0), 22: (99.0, 99.5, 97.5, 98.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[22], trail=True)
    t = one(res)
    assert t["stop"] == t["initial_stop"] and t["stops_raised"] == 0 and res.raises.empty


def test_fixed_stop_option_never_raises():
    path = {21: (100.0, 101.0, 99.0, 100.0), 22: (100.0, 112.0, 99.5, 110.0)}
    res = rp.replay(
        log_rows(20),
        {"AAA": bars(path), "SPY": spy()},
        {"AAA": "Tech", "SPY": "ETF"},
        through=DAYS[22],
        trail=False,
    )
    assert res.raises.empty and one(res)["stop"] == one(res)["initial_stop"]


def test_raise_messages_say_old_new_why_and_the_profit_locked():
    path = {21: (100.0, 101.0, 99.0, 100.0), 22: (100.0, 112.0, 99.5, 110.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[22], trail=True)
    s = rp.summary(res.trades, res.equity)
    title, text = rp.raise_message(res.raises.iloc[0].to_dict(), s)
    assert "raised" in title.lower() and "AAA" in title
    assert "from $" in text and " to $" in text and "best close" in text
    assert "still makes at least about $" in text and "no real order" in text.lower()
    _plain(text)
    loss = rp.raise_message({**res.raises.iloc[0].to_dict(), "new_stop": 97.0}, s)[1]
    assert "loss is limited to about $" in loss


def test_send_all_tells_each_raise_once_in_order_and_marks_flags():
    path = {
        21: (100.0, 101.0, 99.0, 100.0),
        22: (100.0, 112.0, 99.5, 110.0),
        23: (110.0, 111.0, 105.0, 107.0),
    }
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, trail=True)
    sent: list[str] = []
    s = rp.summary(res.trades, res.equity)
    trades, raises, n = rp.send_all(res.trades, res.raises, s, lambda a, b: sent.append(a))
    assert [t.split(":")[0] for t in sent] == [
        "Practice buy",
        "Practice stop raised",
        "Practice sell",
    ]
    assert n == 3 and (raises["sent"] == 1).all()
    _, _, n2 = rp.send_all(trades, raises, s, lambda a, b: sent.append(a))
    assert n2 == 0 and len(sent) == 3
    merged = rp.merge_sent_raises(res.raises, raises)
    assert (merged["sent"] == 1).all()


def test_buy_message_and_daily_holdings_show_the_stop():
    path = {21: (100.0, 101.0, 99.0, 100.0), 22: (100.0, 112.0, 99.5, 110.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[22], trail=True)
    s = rp.summary(res.trades, res.equity, res.marks)
    assert "starts at $96" in rp.buy_message(res.trades.iloc[0].to_dict(), s)[1]
    assert (
        "Stop-loss" in rp.daily_message(DAYS[22], res.trades, s)[1]
        and "$106" in rp.daily_message(DAYS[22], res.trades, s)[1]
    )


def test_default_is_the_simple_fixed_stop_until_the_owner_switches_trailing_on():
    assert rp.TRAIL_STOP is False
    path = {21: (100.0, 101.0, 99.0, 100.0), 22: (100.0, 112.0, 99.5, 110.0)}
    res = run(log_rows(20), {"AAA": bars(path), "SPY": spy()}, through=DAYS[22])
    assert res.raises.empty and one(res)["stop"] == one(res)["initial_stop"]


def test_every_rules_version_has_a_plain_english_change_line():
    assert rp.RULES_VERSION in rp.RULES_CHANGES


def test_upgrade_message_only_when_the_version_changes():
    assert rp.upgrade_message(None, "v2") is None  # first run: nothing to compare
    assert rp.upgrade_message("v2", "v2") is None
    title, text = rp.upgrade_message("v1", "v2")
    assert title == "Council of Ricks upgraded to v2"
    assert "changed from v1 to v2" in text and "Skeptic" in text and "already open" in text
    assert "no real order" in text.lower()
    assert "change log" in rp.upgrade_message("v2", "v9")[1]  # an unknown version still tells us
