# Author: Kai0421
"""Tests for bot.journal: the SQLite bot state (docs/05-memory.md layer 1)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from bot.journal import Journal, PlanRow, PositionRow


@pytest.fixture
def j(tmp_path):
    return Journal(tmp_path / "bot.db")


def ts(h=12, m=0, day=5):
    return datetime(2026, 10, day, h, m, tzinfo=UTC)


def test_schema_is_created_and_reopenable(tmp_path):
    path = tmp_path / "sub" / "bot.db"
    Journal(path).set_flag("paused", True, "test", ts())
    assert Journal(path).flags()["paused"].value is True


# ------------------------------------------------------- runs


def test_runs_lifecycle(j):
    run = j.start_run("premarket", ts(8, 30))
    j.finish_run(run, "ok", ts(8, 31))
    failed = j.start_run("entries", ts(9, 45))
    j.finish_run(failed, "error", ts(9, 46), error="boom")
    assert j.last_runs() == {"premarket": "ok", "entries": "error"}
    assert j.jobs_completed_on(date(2026, 10, 5)) == {"premarket"}
    assert j.slot_done("premarket", "2026-10-05T08:30") is False
    j.mark_slot("premarket", "2026-10-05T08:30")
    assert j.slot_done("premarket", "2026-10-05T08:30") is True


# ------------------------------------------------------- flags


def test_flags(j):
    assert j.flags() == {}
    j.set_flag("paused", True, "drawdown -10%", ts())
    j.set_flag("paused", False, "manual resume", ts(13))
    f = j.flags()["paused"]
    assert f.value is False and f.reason == "manual resume"
    assert not j.flag("paused")
    assert not j.flag("missing")


# ------------------------------------------------------- positions and plans


def pos(**kw):
    base = dict(
        symbol="AAPL",
        entry_price=100.0,
        shares=50,
        shares_initial=50,
        initial_stop=96.0,
        stop=96.0,
        entry_date=date(2026, 10, 5),
        days_held=0,
        partial_taken=False,
        signature="up|mid|strong|deep|near",
        win_prob=0.74,
        sector="Information Technology",
        stop_order_id="stop-1",
        pending_exit=None,
        realized_proceeds=0.0,
    )
    base.update(kw)
    return PositionRow(**base)


def test_positions_roundtrip_and_delete(j):
    j.upsert_position(pos())
    j.upsert_position(pos(stop=100.0, pending_exit=("partial_profit", 25)))
    got = j.positions()["AAPL"]
    assert got.stop == 100.0
    assert got.pending_exit == ("partial_profit", 25)
    assert got.entry_date == date(2026, 10, 5)
    j.delete_position("AAPL")
    assert j.positions() == {}


def test_plans_replace_per_day(j):
    p = PlanRow(
        day=date(2026, 10, 6),
        rank=1,
        symbol="MSFT",
        signature="sig",
        win_prob=0.72,
        atr=5.0,
        momentum=0.2,
        sector="Information Technology",
        last_close=400.0,
    )
    j.save_plans(date(2026, 10, 6), [p, PlanRow(**{**p.__dict__, "rank": 2, "symbol": "AAPL"})])
    j.save_plans(date(2026, 10, 6), [p])
    assert [x.symbol for x in j.plans(date(2026, 10, 6))] == ["MSFT"]
    assert j.plans(date(2026, 10, 7)) == []


# ------------------------------------------------------- signals, orders, trades


def test_signals_and_rejection_counts(j):
    d = date(2026, 10, 5)
    j.record_signal(d, "AAPL", "sig", 0.72, 0.66, 150, True, "planned", {"rsi": 4.2})
    j.record_signal(d, "MSFT", "sig", 0.60, 0.55, 150, False, "probability gate", {})
    j.record_signal(d, "JPM", "sig2", None, None, 0, False, "probability gate", {})
    j.record_signal(d, "XOM", "sig2", None, None, 0, False, "no slots", {})
    assert j.rejection_counts(d, d) == {"probability gate": 2, "no slots": 1}
    assert len(j.signals(d, d)) == 4


def test_orders_upsert(j):
    j.record_order(
        "2026-10-05-AAPL-entry", "a1", "AAPL", "buy", 10, "limit", "new", ts(), 100.2, 96.0
    )
    j.record_order("2026-10-05-AAPL-entry", "a1", "AAPL", "buy", 10, "limit", "filled", ts(10))
    orders = j.orders_on(date(2026, 10, 5))
    assert len(orders) == 1 and orders[0]["status"] == "filled"
    assert orders[0]["limit_price"] == 100.2


def test_trades_and_live_outcomes(j):
    for i, pnl in enumerate([10.0, -5.0, 3.0]):
        j.record_trade(
            symbol=f"S{i}",
            entry_date=date(2026, 10, 1),
            exit_date=date(2026, 10, 2 + i),
            entry_price=100.0,
            exit_price=101.0,
            shares=10,
            pnl=pnl,
            r_multiple=pnl / 40,
            exit_reason="stop",
            signature="sig" if i < 2 else "other",
            win_prob=0.7,
        )
    trades = j.trades()
    assert [t["symbol"] for t in trades] == ["S0", "S1", "S2"]
    assert trades[0]["holding_days"] == 1
    assert j.outcomes_by_signature() == {"sig": [True, False], "other": [True]}
    assert len(j.trades(since=date(2026, 10, 3))) == 2


# ------------------------------------------------------- equity, cash, alerts


def test_equity_history_and_peak(j):
    j.record_equity(date(2026, 10, 1), 100_000, 100_000)
    j.record_equity(date(2026, 10, 2), 102_000, 50_000)
    j.record_equity(date(2026, 10, 2), 101_000, 50_000)  # rerun replaces the day
    j.record_equity(date(2026, 10, 5), 99_000, 49_000)
    hist = j.equity_history()
    assert list(hist["equity"]) == [100_000, 101_000, 99_000]
    assert j.peak_equity() == 101_000
    assert j.previous_equity(date(2026, 10, 5)) == 101_000
    assert j.previous_equity(date(2026, 10, 1)) is None
    assert hist["drawdown"].iloc[-1] == pytest.approx(1 - 99_000 / 101_000)


def test_unsettled_proceeds(j):
    j.add_unsettled(1_500.0, settles_on=date(2026, 10, 6))
    j.add_unsettled(500.0, settles_on=date(2026, 10, 7))
    assert j.unsettled_total(date(2026, 10, 5)) == 2_000.0
    assert j.unsettled_total(date(2026, 10, 6)) == 500.0  # settled on the morning of the 6th
    assert j.unsettled_total(date(2026, 10, 7)) == 0.0


def test_alert_dedupe_window(j):
    now = ts()
    assert j.should_send("k", now, timedelta(minutes=30))
    assert not j.should_send("k", now + timedelta(minutes=10), timedelta(minutes=30))
    assert j.should_send("k", now + timedelta(minutes=31), timedelta(minutes=30))
    assert j.once("usage:7d:75:123")
    assert not j.once("usage:7d:75:123")


def test_kv(j):
    assert j.get_kv("slack_thread:weekly") is None
    j.set_kv("slack_thread:weekly", "123.456")
    assert j.get_kv("slack_thread:weekly") == "123.456"


def test_latest_order_for_symbol(j):
    assert j.latest_order("AAPL", "buy") is None
    j.record_order("2026-10-02-AAPL-entry", "a0", "AAPL", "buy", 5, "limit", "filled", ts(day=2))
    j.record_order(
        "2026-10-05-AAPL-entry", "a1", "AAPL", "buy", 10, "limit", "new", ts(), 100.2, 96.0
    )
    j.record_order("2026-10-05-AAPL-exit", "a2", "AAPL", "sell", 10, "market", "new", ts(13))
    assert j.latest_order("AAPL", "buy")["client_order_id"] == "2026-10-05-AAPL-entry"
    assert j.has_order("2026-10-05-AAPL-exit") and not j.has_order("nope")
