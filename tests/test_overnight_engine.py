# Author: Kai0421
"""Tests for overnight mode (``TRADING_MODE=overnight``, bot.overnight_live).

Buy in the closing auction, stop placed once filled, sell in the next opening auction.
Uses the stateful fake broker of test_engine plus closing/opening auction orders.
"""

import dataclasses
from datetime import date, datetime

import pandas as pd
import pytest

from bot import decide
from bot.broker import BrokerError, BrokerPosition
from bot.calendar import MarketCalendar
from bot.config import RiskConfig, Settings
from bot.engine import Engine
from bot.journal import Journal
from bot.notify import Level
from bot.overnight_hold import OvernightConfig
from test_engine import ET, FakeBroker, FakeNotifier, ohlcv, trend

FRI = date(2026, 10, 2)
MON = date(2026, 10, 5)
TUE = date(2026, 10, 6)


def at(h, m=0, day=MON):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


def chips(end=FRI):
    # open == close in ohlcv, so each night returns the daily step: NVDA > AMD > MU.
    return {
        "NVDA": ohlcv(trend(n=60, step=0.004), end),
        "AMD": ohlcv(trend(n=60, step=0.003), end),
        "MU": ohlcv(trend(n=60, step=0.002), end),
    }


class AuctionBroker(FakeBroker):
    """FakeBroker plus late-day overnight buys."""

    def __init__(self):
        super().__init__()
        self.refuse_sell = False

    def sell_market(self, symbol, qty, cid):
        if self.refuse_sell:
            raise BrokerError("sell rejected")
        return super().sell_market(symbol, qty, cid)

    def submit_close_buy(self, symbol, qty, limit, stop, cid):
        assert 0 < stop < limit  # the stop goes in with the buy
        self.calls.append(("close_buy", symbol, qty, limit, cid))
        o = self._order(cid, symbol, "buy", qty, "limit", limit=limit)
        self.orders.append(o)
        return o

    def fill_close_buys(self, price):
        for o in [o for o in self.orders if o.side == "buy"]:
            self.orders.remove(o)
            self.closed.append(dataclasses.replace(
                o, status="filled", filled_qty=o.qty, filled_avg_price=price,
                filled_at=at(16)))  # fmt: skip
            self.pos[o.symbol] = BrokerPosition(o.symbol, int(o.qty), 0, price, price, "us_equity")
            self.cash -= o.qty * price


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        dry_run=False,
        db_path=tmp_path / "bot.db",
        trading_mode="overnight",
        strategy_name="overnight_hold",
        risk=RiskConfig(risk_per_trade=0.0025, max_position_pct=0.05, max_open_positions=3),
    )
    state = {"now": at(15, 50), "bars": chips(),
             "prices": {"NVDA": 100.0, "AMD": 100.0, "MU": 100.0}}  # fmt: skip
    broker = AuctionBroker()
    notifier = FakeNotifier()
    engine = Engine(
        settings=settings,
        broker=broker,
        journal=Journal(settings.db_path),
        notifier=notifier,
        calendar=MarketCalendar(),
        load_bars=lambda syms, start, end: {
            s: b.loc[: pd.Timestamp(end)] for s, b in state["bars"].items() if s in syms
        },
        latest_prices=lambda syms: {s: p for s, p in state["prices"].items() if s in syms},
        clock=lambda: state["now"],
        universe={"SPY": "Broad Market"},
        evidence_path=tmp_path / "evidence.json",
        memory_dir=tmp_path / "memory",
        lock_path=tmp_path / "engine.lock",
    )
    return dataclasses.make_dataclass("Env", ["engine", "broker", "notifier", "state"])(
        engine, broker, notifier, state
    )


def buys(env):
    return [c for c in env.broker.calls if c[0] == "close_buy"]


# ------------------------------------------------------- schedule


def test_schedule_replaces_entries_and_manage():
    eng = Engine.__new__(Engine)
    eng.settings = Settings(trading_mode="overnight", strategy_name="overnight_hold")
    eng.calendar = MarketCalendar()
    jobs = {j: (s, e) for j, s, e in eng._schedule(MON)}
    assert set(jobs) == {"premarket", "open_sell", "open_check", "close_buy", "eod",
                         "usage_watch", "review"}  # fmt: skip
    assert jobs["close_buy"] == (datetime(1, 1, 1, 15, 50).time(), datetime(1, 1, 1, 15, 56).time())
    # The sale is a plain market order after the open (Alpaca's open-auction order expired
    # unfilled on 2026-10-13), and the backup check starts right after it.
    assert jobs["open_sell"][0] >= datetime(1, 1, 1, 9, 30).time()
    assert jobs["open_sell"][1] < jobs["open_check"][0]
    early = {j: s for j, s, _ in eng._schedule(date(2026, 11, 27))}
    assert early["close_buy"].hour == 12 and early["close_buy"].minute == 50


# ------------------------------------------------------- close buy


def test_close_buy_sizes_top_picks_with_cash_checks(env):
    env.engine.run("close_buy")
    placed = buys(env)
    # Best overnight momentum first; the 2-per-sector limit stops the third chip stock.
    assert [c[1] for c in placed] == ["NVDA", "AMD"]
    sym, qty, limit, cid = placed[0][1:]
    assert limit == 101.0 and cid == f"{MON}-NVDA-entry-close"
    assert qty * limit <= 0.05 * 100_000  # 5% cap per stock
    assert ("verify", True) in env.broker.calls  # cash-only lock re-applied before buying
    sigs = env.engine.journal.signals(MON, MON)
    assert {s["symbol"]: s["reason"] for s in sigs} == {
        "NVDA": "bought at the close",
        "AMD": "bought at the close",
        "MU": "not approved",
    }
    assert [p.symbol for p in env.engine.journal.plans(MON)] == ["NVDA", "AMD"]


def test_close_buy_runs_once_per_symbol(env):
    env.engine.run("close_buy")
    env.state["now"] = at(15, 55)
    env.engine.run("close_buy")
    assert len(buys(env)) == 2


def test_close_buy_respects_the_kill_switch(env):
    env.engine.journal.set_flag("paused", True, "test", at(9))
    env.engine.run("close_buy")
    assert buys(env) == []


def test_close_buy_skips_early_close_days(env):
    env.state["now"] = at(12, 40, date(2026, 11, 27))
    env.state["bars"] = chips(date(2026, 11, 25))
    env.engine.run("close_buy")
    assert buys(env) == []


def test_dry_run_records_but_sends_nothing(env):
    env.broker.dry_run = True
    env.broker.submit_close_buy = lambda *a: None  # Broker returns None in a dry run
    env.engine.run("close_buy")
    rows = env.engine.journal.orders_on(MON)
    assert {r["symbol"] for r in rows} == {"NVDA", "AMD"}
    assert {r["status"] for r in rows} == {"dry_run"}
    assert env.broker.orders == []


# ------------------------------------------------------- the night


def bought(env, price=100.0):
    env.engine.run("close_buy")
    env.broker.fill_close_buys(price)
    env.state["now"] = at(16, 15)
    env.engine.run("eod")


def test_eod_places_the_stop_on_each_fill(env):
    bought(env)
    stops = {o.symbol: o for o in env.broker.orders if o.type == "stop"}
    assert set(stops) == {"NVDA", "AMD"}
    assert stops["NVDA"].stop_price == round(101.0 * 0.95, 2)
    assert stops["NVDA"].qty == env.broker.pos["NVDA"].qty
    assert not any("held since before today" in t for _, _, t in env.notifier.sent)
    assert "NVDA: stop-loss placed" in env.notifier.titles(Level.INFO)
    assert not env.notifier.titles(Level.CRITICAL)


def test_open_sell_sells_at_market_just_after_the_open(env):
    bought(env)
    env.state["now"] = at(9, 32, TUE)
    env.engine.run("open_sell")
    sells = [c for c in env.broker.calls if c[0] == "sell"]
    assert sorted((c[1], c[3]) for c in sells) == [("AMD", f"{TUE}-AMD-exit-open"),
                                                   ("NVDA", f"{TUE}-NVDA-exit-open")]  # fmt: skip
    assert not [c for c in env.broker.calls if c[0] == "open_sell"]  # no open-auction order
    assert not [o for o in env.broker.orders if o.type == "stop"]  # the sale replaces the stop
    env.engine.run("open_sell")  # a second run must not sell twice
    assert len([c for c in env.broker.calls if c[0] == "sell"]) == 2
    assert "Selling NVDA" in env.notifier.titles(Level.INFO)


def test_open_sell_does_nothing_before_the_market_opens_and_keeps_the_stop(env):
    bought(env)
    env.state["now"] = at(9, 0, TUE)
    env.engine.run("open_sell")
    assert not [c for c in env.broker.calls if c[0] in ("sell", "open_sell", "cancel")]
    assert {o.symbol for o in env.broker.orders if o.type == "stop"} == {"NVDA", "AMD"}


def test_refused_open_sell_puts_the_stop_back(env):
    bought(env)
    env.broker.refuse_sell = True
    env.state["now"] = at(9, 32, TUE)
    env.engine.run("open_sell")
    assert {o.symbol for o in env.broker.orders if o.type == "stop"} == {"NVDA", "AMD"}
    assert "Couldn't sell NVDA" in env.notifier.titles(Level.CRITICAL)


def test_open_fill_is_booked_as_a_trade(env):
    bought(env)
    env.state["now"] = at(9, 32, TUE)
    env.engine.run("open_sell")
    for o in [o for o in env.broker.orders if o.side == "sell"]:
        env.broker.fill_sell(o.id, 102.0)
    env.state["now"] = at(9, 35, TUE)
    env.engine.run("open_check")
    trades = env.engine.journal.trades(since=MON)
    assert {t["symbol"] for t in trades} == {"NVDA", "AMD"}
    assert {t["exit_reason"] for t in trades} == {"open"}
    assert all(t["pnl"] > 0 for t in trades)
    assert env.engine.journal.positions() == {}


def test_open_check_sells_anything_the_open_missed(env):
    bought(env)
    env.state["now"] = at(9, 35, TUE)  # open_sell never ran (laptop asleep)
    env.engine.run("open_check")
    late = [c for c in env.broker.calls if c[0] == "sell"]
    assert {c[1] for c in late} == {"NVDA", "AMD"}
    assert all(c[3].endswith("-exit-late") for c in late)
    assert "NVDA didn't sell at the open" in env.notifier.titles(Level.WARN)


def test_tonights_buys_are_not_sold_by_the_open_jobs(env):
    bought(env)
    env.state["now"] = at(9, 0, TUE)
    rows = env.engine.journal.positions()
    book = env.engine.data.book()
    assert decide.overnight_sells(book.positions, rows, book.open_orders, MON) == []


# ------------------------------------------------------- picks (pure)


def test_picks_use_only_bars_through_the_previous_session():
    bars = chips(MON)  # includes Monday's bar, which must be ignored
    bars["MU"].loc[pd.Timestamp(MON), "open"] *= 10  # huge "night" only Monday knows
    picks = decide.overnight_picks(bars, FRI, MON, OvernightConfig(max_positions=1))
    assert picks == ["NVDA"]


def test_picks_honour_notfri_and_refuse_same_day_filters():
    cfg = OvernightConfig(night_filter="notfri")
    assert decide.overnight_picks(chips(date(2026, 10, 1)), date(2026, 10, 1), FRI, cfg) == []
    with pytest.raises(ValueError, match="today's close"):
        decide.overnight_picks(chips(), FRI, MON, OvernightConfig(night_filter="down"))


def test_overnight_buy_prices():
    b = decide.overnight_buy("NVDA", 1, 200.0, MON, "Information Technology")
    assert (b.limit, b.stop, b.cid) == (202.0, 191.9, f"{MON}-NVDA-entry-close")


def test_premarket_note_counts_the_sector_limit(env):
    env.state["now"] = at(8, 30)
    env.engine.run("premarket")
    text = " ".join(t for _, _, t in env.notifier.sent)
    assert "buy up to 2 of MU" in text
