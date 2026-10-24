# Author: Kai0421
"""Tests for bot.engine: the deterministic job runner (premarket, entries, manage, eod, tick).

Uses a stateful fake broker, the 2026 NYSE fallback calendar and synthetic bars in which
AAPL has a Trend Pullback setup on Friday 2026-10-02.
"""

import dataclasses
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot.backtest import prepare
from bot.broker import AccountSnapshot, BrokerError, BrokerOrder, BrokerPosition, ClockInfo
from bot.calendar import MarketCalendar
from bot.config import Settings, StrategyConfig
from bot.engine import Engine
from bot.evidence import Evidence, SignatureEvidence, save_evidence
from bot.journal import Journal, PlanRow, PositionRow
from bot.notify import Delivery, Level
from bot.risk import Account

ET = ZoneInfo("America/New_York")
MON = date(2026, 10, 5)
FRI = date(2026, 10, 2)
UNIVERSE = {
    "AAPL": "Information Technology",
    "MSFT": "Information Technology",
    "SPY": "Broad Market",
}


def at(h, m=0, day=MON):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


# ------------------------------------------------------- synthetic bars


def ohlcv(closes, end=FRI, spread=0.005, volume=2_000_000):
    c = np.asarray(closes, dtype="float64")
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(c))
    return pd.DataFrame(
        {
            "open": c,
            "high": c * (1 + spread),
            "low": c * (1 - spread),
            "close": c,
            "volume": volume,
        },
        index=idx,
    )


def trend(n=300, start=100.0, step=0.003):
    return [start * (1 + step) ** i for i in range(n)]


def pullback(n=300, start=100.0):
    c = trend(n - 4, start)
    for _ in range(3):
        c.append(c[-1] * 0.98)
    c.append(c[-1] * 1.03)
    return c


def market(end=FRI):
    return {
        "AAPL": ohlcv(pullback(), end),
        "MSFT": ohlcv(trend(step=0.002), end),
        "SPY": ohlcv(trend(), end, spread=0.003),
    }


BARS = market()
FEATURES, TABLES = prepare(BARS, StrategyConfig())
AAPL_SIG = TABLES["AAPL"]["signature"].iloc[-1]
AAPL_ATR = float(FEATURES["AAPL"]["atr"].iloc[-1])
assert TABLES["AAPL"]["setup"].iloc[-1] and not TABLES["MSFT"]["setup"].iloc[-1]


# ------------------------------------------------------- fakes


class FakeBroker:
    def __init__(self):
        self.dry_run = False
        self.cash = 100_000.0
        self.equity = 100_000.0
        self.lock_ok = True
        self.fail_account = False
        self.is_open = True
        self.pos: dict[str, BrokerPosition] = {}
        self.orders: list[BrokerOrder] = []
        self.closed: list[BrokerOrder] = []
        self.calls = []
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def account(self):
        if self.fail_account:
            raise BrokerError("get_account failed")
        return AccountSnapshot(Account(self.cash, self.cash, self.equity), False, False, "active")

    def clock(self):
        t = datetime(2026, 10, 5, 13, 0, tzinfo=ET)
        return ClockInfo(t, self.is_open, t, t)

    def positions(self):
        return dict(self.pos)

    def open_orders(self):
        return list(self.orders)

    def closed_orders(self, symbol, after):
        return [o for o in self.closed if o.symbol == symbol]

    def order_by_client_id(self, cid):
        return next((o for o in self.orders + self.closed if o.client_order_id == cid), None)

    def check_asset(self, symbol):
        return None

    def verify_cash_only(self, apply):
        self.calls.append(("verify", apply))
        return self.lock_ok

    def _order(self, cid, symbol, side, qty, type_, limit=None, stop=None, legs=(), cls="simple"):
        return BrokerOrder(
            id=self._id("o"),
            client_order_id=cid,
            symbol=symbol,
            side=side,
            qty=qty,
            filled_qty=0.0,
            filled_avg_price=None,
            type=type_,
            order_class=cls,
            limit_price=limit,
            stop_price=stop,
            status="new",
            legs=tuple(legs),
            submitted_at=at(9, 45),
            filled_at=None,
        )

    def submit_entry(self, symbol, qty, limit, stop, cid):
        self.calls.append(("entry", symbol, qty, limit, stop, cid))
        leg = self._order(cid + "-stop", symbol, "sell", qty, "stop", stop=stop)
        leg = dataclasses.replace(leg, status="held")
        o = self._order(cid, symbol, "buy", qty, "limit", limit=limit, legs=[leg], cls="oto")
        self.orders.append(o)
        return o

    def submit_stop(self, symbol, qty, stop, cid):
        self.calls.append(("stop", symbol, qty, stop, cid))
        o = self._order(cid, symbol, "sell", qty, "stop", stop=stop)
        self.orders.append(o)
        return o

    def sell_market(self, symbol, qty, cid):
        self.calls.append(("sell", symbol, qty, cid))
        o = self._order(cid, symbol, "sell", qty, "market")
        self.orders.append(o)
        return o

    def replace_stop(self, order_id, stop_price=None, qty=None):
        self.calls.append(("replace", order_id, stop_price, qty))
        for i, o in enumerate(self.orders):
            if o.id == order_id:
                self.orders[i] = dataclasses.replace(
                    o, stop_price=stop_price or o.stop_price, qty=qty or o.qty
                )

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        self.orders = [o for o in self.orders if o.id != order_id]

    # -- test helpers

    def fill_entry(self, symbol, price):
        entry = next(o for o in self.orders if o.symbol == symbol and o.side == "buy")
        self.orders.remove(entry)
        self.closed.append(
            dataclasses.replace(
                entry, status="filled", filled_qty=entry.qty, filled_avg_price=price
            )
        )
        leg = dataclasses.replace(entry.legs[0], status="new")
        self.orders.append(leg)
        qty = int(entry.qty)
        self.pos[symbol] = BrokerPosition(symbol, qty, 0, price, price, "us_equity")
        self.cash -= qty * price

    def fill_sell(self, order_id, price):
        o = next(o for o in self.orders if o.id == order_id)
        self.orders.remove(o)
        self.closed.append(
            dataclasses.replace(
                o, status="filled", filled_qty=o.qty, filled_avg_price=price, filled_at=at(14)
            )
        )
        p = self.pos[o.symbol]
        left = p.qty - int(o.qty)
        if left:
            self.pos[o.symbol] = dataclasses.replace(p, qty=left)
        else:
            del self.pos[o.symbol]
        self.cash += o.qty * price


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.categories = []

    def notify(self, level, title, text="", files=(), thread=None, category="bot"):
        self.sent.append((level, title, text))
        self.categories.append(category)
        return Delivery(True, "ts-1")

    def levels(self):
        return [s[0] for s in self.sent]

    def titles(self, level=None):
        return [s[1] for s in self.sent if level is None or s[0] == level]


@pytest.fixture
def env(tmp_path):
    settings = Settings(dry_run=False, db_path=tmp_path / "bot.db")
    state = {"now": at(8, 30), "bars": BARS, "prices": {"AAPL": 120.0, "MSFT": 110.0}}
    broker = FakeBroker()
    notifier = FakeNotifier()
    journal = Journal(settings.db_path)
    evidence_path = tmp_path / "evidence.json"
    save_evidence(
        evidence_path,
        Evidence(
            {AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "bt1", "ACCEPTED"
        ),
    )

    def load_bars(symbols, start, end):
        return {s: df for s, df in state["bars"].items() if s in symbols}

    engine = Engine(
        settings=settings,
        broker=broker,
        journal=journal,
        notifier=notifier,
        calendar=MarketCalendar(),
        load_bars=load_bars,
        latest_prices=lambda symbols: {s: p for s, p in state["prices"].items() if s in symbols},
        clock=lambda: state["now"],
        universe=UNIVERSE,
        evidence_path=evidence_path,
        memory_dir=tmp_path / "memory",
        lock_path=tmp_path / "engine.lock",
    )
    return dataclasses.make_dataclass(
        "Env", ["engine", "broker", "journal", "notifier", "state", "tmp"]
    )(engine, broker, journal, notifier, state, tmp_path)


def plan(symbol="AAPL", rank=1, atr=AAPL_ATR, sector="Information Technology"):
    return PlanRow(MON, rank, symbol, AAPL_SIG, 0.8, atr, 0.3, sector, 119.0)


# ------------------------------------------------------- run wrapper


def test_run_records_success_and_failure(env):
    assert env.engine.run("premarket") == "ok"
    env.engine.premarket = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert env.engine.run("premarket") == "error"
    assert env.journal.last_runs()["premarket"] == "error"
    assert Level.WARN in env.notifier.levels()


def test_unknown_job_rejected(env):
    with pytest.raises(ValueError):
        env.engine.run("buy_everything")


# ------------------------------------------------------- premarket


def test_premarket_plans_gated_setup(env):
    env.engine.premarket()
    plans = env.journal.plans(MON)
    assert [p.symbol for p in plans] == ["AAPL"]
    assert plans[0].win_prob == pytest.approx(0.8)
    assert plans[0].atr == pytest.approx(AAPL_ATR)
    sig = env.journal.signals(MON, MON)
    assert sig[0]["symbol"] == "AAPL" and sig[0]["taken"] == 1
    assert "earnings" in json.loads(sig[0]["features"])["flags"][0]  # policy "flag"


def test_premarket_strict_earnings_policy_rejects(env):
    env.engine.settings = dataclasses.replace(env.engine.settings, earnings_policy="strict")
    env.engine.premarket()
    assert env.journal.plans(MON) == []
    assert "earnings" in env.journal.signals(MON, MON)[0]["reason"]


def test_premarket_without_evidence_plans_nothing(env):
    env.engine.evidence_path.unlink()
    env.engine.premarket()
    assert env.journal.plans(MON) == []
    assert any("backtest" in t.lower() for t in env.notifier.titles(Level.WARN))


def test_premarket_rejected_backtest_plans_nothing(env):
    save_evidence(
        env.engine.evidence_path,
        Evidence({AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", "REJECTED"),
    )
    env.engine.premarket()
    assert env.journal.plans(MON) == []
    assert "not accepted" in env.journal.signals(MON, MON)[0]["reason"]


def test_premarket_paper_trial_plans_despite_a_rejected_backtest(env):
    save_evidence(
        env.engine.evidence_path,
        Evidence({AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", "REJECTED"),
    )
    env.engine.settings = dataclasses.replace(env.engine.settings, paper_trial=True)
    env.engine.premarket()
    assert [p.symbol for p in env.journal.plans(MON)] == ["AAPL"]
    assert json.loads(env.journal.signals(MON, MON)[0]["features"])["paper_trial"] is True
    assert "paper trial" in premarket_summary(env)[2]


def test_premarket_uses_the_strategys_own_evidence_file(env):
    save_evidence(
        env.engine.evidence_path.with_name("trend_pullback.json"),
        Evidence({AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", "REJECTED"),
    )
    env.engine.premarket()
    assert env.journal.plans(MON) == []
    assert "not accepted" in env.journal.signals(MON, MON)[0]["reason"]


def test_premarket_refuses_evidence_for_another_strategy(env):
    env.engine.settings = dataclasses.replace(env.engine.settings, strategy_name="rsi2_reversion")
    env.engine.premarket()
    assert env.journal.plans(MON) == []
    assert any("different strategy" in t for t in env.notifier.titles(Level.WARN))


def test_premarket_refuses_sp100_evidence_when_trading_sp500(env):
    env.engine.settings = dataclasses.replace(env.engine.settings, universe_name="sp500")
    env.engine.premarket()
    assert env.journal.plans(MON) == []
    assert any("different stock list" in t for t in env.notifier.titles(Level.WARN))


def test_premarket_uses_the_universes_own_evidence_file(env):
    env.engine.settings = dataclasses.replace(env.engine.settings, universe_name="sp500")
    save_evidence(
        env.engine.evidence_path.with_name("trend_pullback-sp500.json"),
        Evidence(
            {AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", "ACCEPTED",
            universe="sp500",
        ),
    )  # fmt: skip
    env.engine.premarket()
    assert [p.symbol for p in env.journal.plans(MON)] == ["AAPL"]


def test_premarket_stale_bars_plans_nothing(env):
    env.state["bars"] = market(end=date(2026, 10, 1))
    env.engine.premarket()
    assert env.journal.plans(MON) == []
    assert any("out of date" in t for t in env.notifier.titles(Level.WARN))


def test_premarket_closed_day_does_nothing(env):
    env.state["now"] = at(8, 30, day=date(2026, 10, 3))  # Saturday
    env.engine.premarket()
    assert env.journal.plans(date(2026, 10, 3)) == []


def test_premarket_paused_plans_nothing(env):
    env.journal.set_flag("paused", True, "drawdown", at(8))
    env.engine.premarket()
    assert env.journal.plans(MON) == []


# ------------------------------------------------------- entries


def entries_env(env, plans=None):
    env.journal.save_plans(MON, plans or [plan()])
    env.state["now"] = at(9, 50)


def test_entries_submit_oto_with_fresh_cash_check(env):
    entries_env(env)
    env.engine.entries()
    (call,) = [c for c in env.broker.calls if c[0] == "entry"]
    _, symbol, qty, limit, stop, cid = call
    assert (symbol, cid) == ("AAPL", "2026-10-05-AAPL-entry")
    assert limit == pytest.approx(120.24)  # last 120 + 0.2%
    assert stop == pytest.approx(round(limit - 2 * AAPL_ATR, 2), abs=0.011)
    assert qty * limit <= 0.20 * 100_000 + 1e-6  # max position size
    assert ("verify", True) in env.broker.calls
    assert env.journal.orders_on(MON)[0]["client_order_id"] == cid


def test_entries_are_idempotent(env):
    entries_env(env)
    env.engine.entries()
    env.engine.entries()
    assert len([c for c in env.broker.calls if c[0] == "entry"]) == 1


def test_entries_blocked_by_margin_lock(env):
    entries_env(env)
    env.broker.lock_ok = False
    env.engine.entries()
    assert not [c for c in env.broker.calls if c[0] == "entry"]
    assert Level.CRITICAL in env.notifier.levels()


def test_entries_blocked_when_account_fetch_fails(env):
    entries_env(env)
    env.broker.fail_account = True
    env.engine.entries()
    assert not [c for c in env.broker.calls if c[0] == "entry"]


@pytest.mark.parametrize("flag", ["paused", "halt_today"])
def test_entries_blocked_by_flags(env, flag):
    entries_env(env)
    env.journal.set_flag(flag, True, "test", at(8))
    if flag == "halt_today":
        env.journal.set_kv("halt_day", MON.isoformat())
    env.engine.entries()
    assert not [c for c in env.broker.calls if c[0] == "entry"]


def test_entries_outside_window(env):
    entries_env(env)
    env.state["now"] = at(9, 35)  # first 15 minutes
    env.engine.entries()
    env.state["now"] = at(11, 30)  # entries are cancelled at 11:00
    env.engine.entries()
    assert not [c for c in env.broker.calls if c[0] == "entry"]


def test_entries_batch_respects_cash(env):
    env.broker.cash = 25_000.0
    env.broker.equity = 100_000.0
    entries_env(env, [plan("AAPL", 1), plan("MSFT", 2, sector="Information Technology")])
    env.state["prices"] = {"AAPL": 120.0, "MSFT": 120.0}
    env.engine.entries()
    entries = [c for c in env.broker.calls if c[0] == "entry"]
    total = sum(c[2] * c[3] for c in entries)
    assert total <= 25_000 - 0.02 * 100_000 + 1e-6


def test_entries_execute_pending_exits_first(env):
    env.state["now"] = at(9, 50)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 100, 0, 100.0, 105.0, "us_equity")
    stop = env.broker.submit_stop("MSFT", 100, 96.0, "s-MSFT")
    env.journal.upsert_position(
        PositionRow("MSFT", 100.0, 100, 100, 96.0, 96.0, FRI, 1, False, AAPL_SIG, 0.8,
                    "Information Technology", stop.id, ("trend_break", 100))
    )  # fmt: skip
    env.engine.entries()
    kinds = [c[0] for c in env.broker.calls]
    assert kinds.index("cancel") < kinds.index("sell")
    assert ("sell", "MSFT", 100, "2026-10-05-MSFT-exit-trend_break") in env.broker.calls
    assert env.journal.positions()["MSFT"].pending_exit is None


def test_partial_exit_reduces_stop_then_sells(env):
    env.state["now"] = at(9, 50)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 100, 0, 100.0, 109.0, "us_equity")
    stop = env.broker.submit_stop("MSFT", 100, 104.8, "s-MSFT")
    env.journal.upsert_position(
        PositionRow("MSFT", 100.0, 100, 100, 96.0, 104.8, FRI, 3, False, AAPL_SIG, 0.8,
                    "Information Technology", stop.id, ("partial_profit", 50))
    )  # fmt: skip
    env.engine.entries()
    assert ("replace", stop.id, None, 50) in env.broker.calls
    assert ("sell", "MSFT", 50, "2026-10-05-MSFT-partial") in env.broker.calls


# ------------------------------------------------------- reconcile / protect


def test_filled_entry_is_adopted_with_stop_leg(env):
    entries_env(env)
    env.engine.entries()
    env.broker.fill_entry("AAPL", 120.1)
    env.state["now"] = at(10, 0)
    env.engine.manage()
    row = env.journal.positions()["AAPL"]
    assert row.entry_price == 120.1
    assert row.signature == AAPL_SIG and row.win_prob == pytest.approx(0.8)
    leg = next(o for o in env.broker.orders if o.symbol == "AAPL" and o.side == "sell")
    assert row.stop_order_id == leg.id and row.initial_stop == leg.stop_price


def test_filled_entry_posts_a_purchase_alert_with_shares_and_price(env):
    entries_env(env)
    env.engine.entries()
    env.broker.fill_entry("AAPL", 120.1)
    env.state["now"] = at(10, 0)
    env.engine.manage()
    row = env.journal.positions()["AAPL"]
    (msg,) = [s for s in env.notifier.sent if s[1] == ":moneybag: Bought AAPL"]
    assert msg[0] is Level.INFO
    assert f"Bought {row.shares} shares of AAPL at $120.10 each" in msg[2]
    assert f"Total: ${row.shares * 120.1:,.2f}" in msg[2]
    assert f"Stop-loss is at ${row.initial_stop:,.2f}" in msg[2]


def test_unprotected_position_gets_stop_and_critical(env):
    env.state["now"] = at(10, 0)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 10, 10, 100.0, 101.0, "us_equity")
    env.engine.manage()
    stops = [c for c in env.broker.calls if c[0] == "stop"]
    assert stops and stops[0][1:3] == ("MSFT", 10)
    assert 0 < stops[0][3] < 101.0
    assert Level.CRITICAL in env.notifier.levels()


def test_closed_position_becomes_trade_with_unsettled_cash(env):
    entries_env(env)
    env.engine.entries()
    env.broker.fill_entry("AAPL", 120.0)
    env.state["now"] = at(10, 0)
    env.engine.manage()
    row = env.journal.positions()["AAPL"]
    env.broker.fill_sell(row.stop_order_id, row.stop)  # stop hit
    env.state["now"] = at(14, 30)
    env.engine.manage()
    assert env.journal.positions() == {}
    (t,) = env.journal.trades()
    assert t["exit_reason"] == "stop"
    (msg,) = [s for s in env.notifier.sent if s[1].endswith("Sold AAPL")]
    assert f"Sold {row.shares} shares of AAPL at ${row.stop:,.2f} each" in msg[2]
    assert "Loss: $" in msg[2] and "the stop-loss was hit" in msg[2]
    assert t["pnl"] == pytest.approx((row.stop - 120.0) * row.shares)
    assert env.journal.unsettled_total(MON) == pytest.approx(row.stop * row.shares)
    assert env.journal.unsettled_total(date(2026, 10, 6)) == 0  # T+1


# ------------------------------------------------------- manage


def test_manage_cancels_unfilled_entries_after_11(env):
    entries_env(env)
    env.engine.entries()
    env.state["now"] = at(10, 30)
    env.engine.manage()
    assert not [c for c in env.broker.calls if c[0] == "cancel"]
    env.state["now"] = at(11, 0)
    env.engine.manage()
    assert [c for c in env.broker.calls if c[0] == "cancel"]


def test_manage_raises_stop_only_up(env):
    env.state["now"] = at(10, 0)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 100, 0, 100.0, 108.0, "us_equity")
    stop = env.broker.submit_stop("MSFT", 100, 96.0, "s-MSFT")
    row = PositionRow("MSFT", 100.0, 100, 100, 96.0, 100.0, FRI, 2, False, AAPL_SIG, 0.8,
                      "Information Technology", stop.id, None)  # fmt: skip
    env.journal.upsert_position(row)
    env.engine.manage()
    assert ("replace", stop.id, 100.0, None) in env.broker.calls
    env.journal.upsert_position(dataclasses.replace(row, stop=90.0))
    n = len(env.broker.calls)
    env.engine.manage()
    assert not [c for c in env.broker.calls[n:] if c[0] == "replace"]


# ------------------------------------------------------- eod


def eod_env(env, equity, prev=100_000.0, peak=100_000.0):
    env.journal.record_equity(date(2026, 9, 1), peak, peak)
    env.journal.record_equity(FRI, prev, prev)
    env.broker.equity = equity
    env.state["now"] = at(16, 20)
    env.state["bars"] = market(end=MON)


def test_eod_records_equity_state_and_pack(env):
    eod_env(env, 100_500.0)
    env.engine.eod()
    hist = env.journal.equity_history()
    assert hist["equity"].iloc[-1] == 100_500.0
    state = json.loads((env.tmp / "memory" / "state.json").read_text())
    assert state["equity"] == 100_500.0 and state["mode"] == "paper"
    pack = env.tmp / "memory" / "packs" / "daily-2026-10-05.md"
    assert pack.exists() and len(pack.read_text()) <= 6_000
    assert any("End of day" in t for t in env.notifier.titles(Level.INFO))


def test_eod_daily_loss_halts_next_session(env):
    eod_env(env, 96_000.0)
    env.engine.eod()
    assert env.journal.flag("halt_today")
    assert env.journal.get_kv("halt_day") == "2026-10-06"
    assert Level.CRITICAL in env.notifier.levels()


def test_eod_drawdown_pauses(env):
    eod_env(env, 89_000.0, prev=90_000.0, peak=100_000.0)
    env.engine.eod()
    assert env.journal.flag("paused")


def test_eod_decides_exits_and_new_stops(env):
    eod_env(env, 100_000.0)
    # AAPL closed today far below its 50 SMA -> trend break tomorrow.
    bars = market(end=MON)
    bars["AAPL"].iloc[-1, bars["AAPL"].columns.get_loc("close")] = 50.0
    bars["AAPL"].iloc[-1, bars["AAPL"].columns.get_loc("low")] = 49.0
    env.state["bars"] = bars
    env.broker.pos["AAPL"] = BrokerPosition("AAPL", 10, 0, 100.0, 50.0, "us_equity")
    env.journal.upsert_position(
        PositionRow("AAPL", 100.0, 10, 10, 40.0, 40.0, FRI, 1, False, AAPL_SIG, 0.8,
                    "Information Technology", None, None)
    )  # fmt: skip
    env.engine.eod()
    row = env.journal.positions()["AAPL"]
    assert row.pending_exit == ("trend_break", 10)
    assert row.days_held == 1  # Fri -> Mon is one session


def test_resume_clears_pause(env):
    env.journal.set_flag("paused", True, "dd", at(8))
    env.engine.resume()
    assert not env.journal.flag("paused")


# ------------------------------------------------------- scheduler


def test_tick_runs_due_jobs_once(env):
    ran = []
    for job in ("premarket", "entries", "manage", "eod", "usage_watch", "review"):
        setattr(env.engine, job, (lambda j: lambda: ran.append(j))(job))
    env.state["now"] = at(8, 35)
    env.engine.tick()
    env.engine.tick()
    assert ran == ["premarket"]
    env.state["now"] = at(9, 47)
    env.engine.tick()
    env.state["now"] = at(10, 2)
    env.engine.tick()
    env.state["now"] = at(10, 33)
    env.engine.tick()
    assert ran == ["premarket", "entries", "manage", "manage"]


def test_tick_skips_missed_windows(env):
    ran = []
    for job in ("premarket", "entries", "manage", "eod", "usage_watch", "review"):
        setattr(env.engine, job, (lambda j: lambda: ran.append(j))(job))
    env.state["now"] = at(16, 30)  # Mac woke late: only eod is still in its window
    env.engine.tick()
    assert ran == ["eod"]


def test_tick_weekend_schedule(env):
    ran = []
    for job in ("weekly_report", "usage_watch", "weekly_review", "premarket"):
        setattr(env.engine, job, (lambda j: lambda: ran.append(j))(job))
    sat = date(2026, 10, 10)
    for h, m in ((9, 5), (9, 55), (10, 10)):
        env.state["now"] = at(h, m, day=sat)
        env.engine.tick()
    assert ran == ["weekly_report", "usage_watch", "weekly_review"]


def test_failed_partial_sell_restores_stop_qty(env):
    env.state["now"] = at(9, 50)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 100, 0, 100.0, 109.0, "us_equity")
    stop = env.broker.submit_stop("MSFT", 100, 104.8, "s-MSFT")
    env.journal.upsert_position(
        PositionRow("MSFT", 100.0, 100, 100, 96.0, 104.8, FRI, 3, False, AAPL_SIG, 0.8,
                    "Information Technology", stop.id, ("partial_profit", 50))
    )  # fmt: skip

    def boom(*a, **k):
        raise BrokerError("sell rejected")

    env.broker.sell_market = boom
    env.engine.entries()
    replaces = [c for c in env.broker.calls if c[0] == "replace"]
    assert replaces == [("replace", stop.id, None, 50), ("replace", stop.id, None, 100)]
    assert Level.CRITICAL in env.notifier.levels()
    assert env.journal.positions()["MSFT"].pending_exit == ("partial_profit", 50)  # retried later


def test_cancelled_entry_recorded_as_canceled(env):
    entries_env(env)
    env.engine.entries()
    env.state["now"] = at(11, 5)
    env.engine.manage()
    assert env.journal.orders_on(MON)[0]["status"] == "canceled"


def pack_fills(env):
    text = (env.tmp / "memory" / "packs" / "daily-2026-10-05.md").read_text()
    if "## Fills\n" not in text:  # empty sections are dropped from packs
        return []
    section = text.split("## Fills\n", 1)[1].split("\n\n## ", 1)[0]
    return [line for line in section.splitlines()[2:] if line.startswith("|")]


def test_daily_pack_fills_skip_entries_cancelled_unfilled(env):
    entries_env(env)
    env.engine.entries()
    env.state["now"] = at(11, 5)
    env.engine.manage()  # stale entry cancelled, never filled
    eod_env(env, 100_000.0)
    env.engine.eod()
    assert pack_fills(env) == []


def test_daily_pack_fills_use_the_real_fill_price(env):
    entries_env(env)
    env.engine.entries()
    qty = int(env.broker.orders[0].qty)
    env.broker.fill_entry("AAPL", 119.87)  # limit was 120.24
    eod_env(env, 100_000.0)
    env.engine.eod()
    assert pack_fills(env) == [f"| AAPL | buy | {qty} | 119.87 |"]


def test_main_without_keys_exits_cleanly(monkeypatch, tmp_path, capsys):
    from bot import engine as engine_mod

    monkeypatch.chdir(tmp_path)
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert engine_mod.main(["status"]) == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_review_skipped_alert_only_when_review_was_fired(env):
    eod_env(env, 100_000.0)
    env.engine.settings = dataclasses.replace(env.engine.settings, routine_daily_url="https://x")
    seen = []
    env.engine.review_exists = lambda day: seen.append(day) or False
    env.engine.eod()
    assert seen == []  # nothing fired on Friday -> nothing to check
    assert "The AI review didn't happen" not in env.notifier.titles(Level.INFO)
    env.journal.set_kv(f"review_fired:{FRI}", "1")
    env.engine.eod()
    assert seen == [FRI]
    assert "The AI review didn't happen" in env.notifier.titles(Level.INFO)


def test_review_found_means_no_alert(env):
    eod_env(env, 100_000.0)
    env.engine.settings = dataclasses.replace(env.engine.settings, routine_daily_url="https://x")
    env.journal.set_kv(f"review_fired:{FRI}", "1")
    env.engine.review_exists = lambda day: True
    env.engine.eod()
    assert "The AI review didn't happen" not in env.notifier.titles(Level.INFO)


def test_fire_records_review_fired(env, monkeypatch):
    monkeypatch.setattr("bot.usage.fire_routine", lambda url, token, text: True)
    env.engine.settings = dataclasses.replace(
        env.engine.settings, routine_daily_url="https://x", routine_daily_token="t"
    )
    env.state["now"] = at(18, 7)
    pack = env.tmp / "memory" / "packs" / f"daily-{MON}.md"
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text("# Daily pack 2026-10-05")
    env.engine.review()
    assert env.journal.get_kv(f"review_fired:{MON}") == "1"


# ------------------------------------------------------- premarket summary (heartbeat)


def premarket_summary(env):
    msgs = [s for s in env.notifier.sent if s[1].startswith(":sunrise: Morning plan")]
    assert len(msgs) == 1, env.notifier.sent
    return msgs[0]


def test_premarket_posts_summary_with_plan(env):
    env.engine.premarket()
    level, title, text = premarket_summary(env)
    assert level is Level.INFO and title == ":sunrise: Morning plan for Mon Oct 5"
    assert (
        "1 possible trade" in text and "Planning 1 buy" in text and "AAPL" in text and "80%" in text
    )
    assert "paper trading" in text  # fixture runs with DRY_RUN=false


def test_premarket_summary_says_dry_run(env):
    env.engine.settings = dataclasses.replace(env.engine.settings, dry_run=True)
    env.engine.premarket()
    assert "dry run" in premarket_summary(env)[2]


def test_premarket_summary_explains_empty_plan(env):
    save_evidence(
        env.engine.evidence_path,
        Evidence({AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", "REJECTED"),
    )
    env.engine.premarket()
    text = premarket_summary(env)[2]
    assert "Planning 0 buys" in text and "not accepted" in text


def test_premarket_summary_when_paused(env):
    env.journal.set_flag("paused", True, "drawdown", at(8))
    env.engine.premarket()
    assert "paused" in premarket_summary(env)[2].lower()


def test_no_premarket_summary_on_closed_days(env):
    env.state["now"] = at(8, 30, day=date(2026, 10, 3))
    env.engine.premarket()
    assert not [s for s in env.notifier.sent if s[1].startswith(":sunrise: Morning plan")]


def test_premarket_summary_groups_rejections(env):
    reasons = {
        "A": "win rate 57.8% below target 65%",
        "B": "win rate 52.8% below target 65%",
        "C": "95% lower bound 61.7% below floor 62%",
    }
    for sym, reason in reasons.items():
        env.journal.record_signal(MON, sym, "s", None, None, 0, False, reason, {})
    env.engine._premarket_summary(MON, None)
    text = premarket_summary(env)[2]
    assert "2 setups because the chance of winning is below the required 65%" in text
    assert "1 setup because past results aren't reliable enough" in text


def test_eod_grades_logged_signals_in_shadow_and_packs_them(env):
    from bot.shadow import shadow_rows

    eod_env(env, 100_000.0)
    day = env.state["bars"]["AAPL"].index[-120].date()
    feats = {"strategy": "trend_pullback", "atr": AAPL_ATR}
    env.journal.record_signal(day, "AAPL", AAPL_SIG, 0.8, 0.7, 0, False, "no slots", feats)
    env.engine.eod()
    rows = shadow_rows(env.journal)
    assert [r["symbol"] for r in rows] == ["AAPL"]
    pack = (env.tmp / "memory" / "packs" / "daily-2026-10-05.md").read_text()
    assert "Shadow trades" in pack


def test_eod_survives_a_shadow_failure(env, monkeypatch):
    import bot.shadow

    def boom(*a, **k):
        raise RuntimeError("bars down")

    monkeypatch.setattr(bot.shadow, "settle", boom)
    eod_env(env, 100_000.0)
    env.engine.eod()
    pack = (env.tmp / "memory" / "packs" / "daily-2026-10-05.md").read_text()
    assert "shadow settle failed" in pack


def test_short_day_has_no_zero_padding_and_works_on_windows():
    from pathlib import Path

    from bot.engine import short_day

    assert short_day(date(2026, 10, 5)) == "Mon Oct 5"
    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [p for p in src.rglob("*.py") if re.search(r"%-[a-zA-Z]", p.read_text("utf-8"))]
    assert offenders == []  # "%-d" style formats raise ValueError on Windows


# ------------------------------------------------------- sentiment exit (opt-in)


def sentiment_env(env, *, on=True, ts="2026-10-05T13:50:00Z", score=20, net=-0.6, vol=70,
                  pending=None, held_flag=False):  # fmt: skip
    """MSFT held with a stop; one Stocktwits snapshot for it in env.tmp/sentiment."""
    from bot import sentiment as st

    env.state["now"] = at(10, 0)  # 14:00 UTC, EDT
    env.engine.settings = dataclasses.replace(env.engine.settings, sentiment_exit=on)
    env.engine.sentiment_dir = env.tmp / "sentiment"
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 100, 0, 100.0, 101.0, "us_equity")
    stop = env.broker.submit_stop("MSFT", 100, 96.0, "s-MSFT")
    env.journal.upsert_position(
        PositionRow("MSFT", 100.0, 100, 100, 96.0, 96.0, FRI, 2, False, AAPL_SIG, 0.8,
                    "Information Technology", stop.id, pending)
    )  # fmt: skip
    st.append_snapshots(
        env.engine.sentiment_dir / "snapshots.jsonl",
        [st.Snapshot(ts=ts, symbol="MSFT", price=101.0, score=score, volume_score=vol,
                     post_net=net, post_tagged=5, held=held_flag)],
    )  # fmt: skip
    return stop


def sells(env):
    return [c for c in env.broker.calls if c[0] == "sell"]


def test_manage_sells_a_held_stock_on_a_fresh_sentiment_warning(env):
    stop = sentiment_env(env)
    env.engine.manage()
    assert ("cancel", stop.id) in env.broker.calls
    assert sells(env) == [("sell", "MSFT", 100, "2026-10-05-MSFT-exit-sentiment")]
    assert any("sentiment" in str(m).lower() for m in env.notifier.sent)


def test_sentiment_exit_is_off_unless_enabled(env):
    sentiment_env(env, on=False)
    env.engine.manage()
    assert sells(env) == []


def test_a_stale_sentiment_snapshot_sells_nothing(env):
    sentiment_env(env, ts="2026-10-05T11:00:00Z")  # 180 minutes old
    env.engine.manage()
    assert sells(env) == []


def test_a_missing_sentiment_file_sells_nothing(env):
    sentiment_env(env)
    (env.engine.sentiment_dir / "snapshots.jsonl").unlink()
    env.engine.manage()
    assert sells(env) == []


@pytest.mark.parametrize("kw", [{"score": 55}, {"net": 0.4}, {"vol": 20}])
def test_a_calm_or_unconfirmed_snapshot_sells_nothing(env, kw):
    sentiment_env(env, **kw)
    env.engine.manage()
    assert sells(env) == []


def test_the_bots_own_book_decides_what_is_held_not_the_collectors_flag(env):
    sentiment_env(env, held_flag=False)  # the collector did not know we hold MSFT
    env.engine.manage()
    assert len(sells(env)) == 1


def test_sentiment_exit_is_sent_once(env):
    sentiment_env(env)
    env.engine.manage()
    env.engine.manage()
    assert len(sells(env)) == 1


def test_a_stock_we_do_not_hold_is_never_sold(env):
    sentiment_env(env)
    env.broker.pos.clear()
    env.engine.manage()
    assert sells(env) == []


def test_sentiment_exit_does_not_double_up_on_a_pending_exit(env):
    sentiment_env(env, pending=("trend_break", 100))
    env.engine.manage()
    assert [c[3] for c in sells(env)] == ["2026-10-05-MSFT-exit-trend_break"]


def test_reports_go_to_the_general_channel_and_trading_alerts_stay_with_the_bot(env):
    for job in ("usage_watch", "review", "weekly_report", "weekly_review"):
        env.engine.run(job)
        env.engine._alert(Level.INFO, "report", "x")
        assert env.notifier.categories[-1] == "general", job
    for job in ("manage", "eod"):
        env.engine.run(job)
        env.engine._alert(Level.INFO, "trade", "x")
        assert env.notifier.categories[-1] == "bot", job


# ------------------------------------------------- automatic backtest refresh


def fake_backtest(env, verdict="ACCEPTED", code=0, as_of=date(2026, 12, 11)):
    calls = []

    def runner(argv):
        calls.append(list(argv))
        if code != 2:  # 2 = the backtest failed before writing anything
            out = Path(argv[argv.index("--evidence-out") + 1])
            save_evidence(out, Evidence({AAPL_SIG: SignatureEvidence(160, 128, 0.3)}, as_of,
                                        "bt2", verdict))  # fmt: skip
        return code

    env.engine.backtest_runner = runner
    return calls


def test_refresh_evidence_skips_fresh_evidence(env):
    calls = fake_backtest(env)
    env.state["now"] = at(11, 0, day=date(2026, 11, 28))  # evidence 59 days old
    env.engine.refresh_evidence()
    assert calls == [] and env.notifier.sent == []


def test_refresh_evidence_reruns_the_backtest_before_it_goes_stale(env):
    calls = fake_backtest(env)
    env.state["now"] = at(11, 0, day=date(2026, 12, 12))  # 73 days old: refresh
    env.engine.refresh_evidence()
    (argv,) = calls
    assert argv[argv.index("--start") + 1] == "2016-01-01"
    assert argv[argv.index("--end") + 1] == "2026-12-11"
    assert argv[argv.index("--strategy") + 1] == "trend_pullback"
    assert argv[argv.index("--universe") + 1] == "sp100"
    assert "--stability" in argv and "--stability-advisory" not in argv
    assert argv[argv.index("--evidence-out") + 1] == str(env.engine.evidence_path)
    assert argv[argv.index("--out") + 1] == str(env.tmp / "memory" / "backtests")
    ev, _ = env.engine.data.evidence("trend_pullback")
    assert ev.as_of == date(2026, 12, 11)
    assert env.notifier.titles(Level.INFO) == ["Backtest refreshed"]


def test_refresh_evidence_keeps_the_owners_stability_advisory(env):
    save_evidence(
        env.engine.evidence_path,
        Evidence({AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "bt1",
                 "ACCEPTED (stability advisory)"),
    )  # fmt: skip
    calls = fake_backtest(env, verdict="ACCEPTED (stability advisory)")
    env.state["now"] = at(11, 0, day=date(2026, 12, 12))
    env.engine.refresh_evidence()
    assert "--stability-advisory" in calls[0]


def test_refresh_evidence_warns_when_the_new_backtest_is_rejected(env):
    fake_backtest(env, verdict="REJECTED", code=1)
    env.state["now"] = at(11, 0, day=date(2026, 12, 12))
    env.engine.refresh_evidence()
    assert env.notifier.titles(Level.WARN) == ["Backtest refreshed but not accepted"]
    env.state["now"] = at(8, 30)
    env.engine.premarket()  # the gate refuses everything, as before: no new swing trades
    assert env.journal.plans(MON) == []


def test_refresh_evidence_failure_raises_so_the_job_alerts(env):
    fake_backtest(env, code=2)
    env.state["now"] = at(11, 0, day=date(2026, 12, 12))
    with pytest.raises(RuntimeError):
        env.engine.refresh_evidence()


def test_refresh_evidence_writes_a_new_universe_beside_the_default_file(env):
    env.engine.settings = dataclasses.replace(env.engine.settings, universe_name="sp500")
    calls = fake_backtest(env)
    env.state["now"] = at(11, 0, day=date(2026, 12, 12))
    env.engine.refresh_evidence()  # no sp500 file yet: the sp100 file is never overwritten
    out = calls[0][calls[0].index("--evidence-out") + 1]
    assert out == str(env.tmp / "trend_pullback-sp500.json")


def test_refresh_evidence_is_skipped_outside_swing_mode(env):
    calls = fake_backtest(env)
    env.engine.settings = dataclasses.replace(
        env.engine.settings, trading_mode="overnight", strategy_name="overnight_hold"
    )
    env.state["now"] = at(11, 0, day=date(2026, 12, 12))
    env.engine.refresh_evidence()
    assert calls == []


def test_refresh_evidence_runs_on_saturday_unless_switched_off(env):
    ran = []
    env.engine.refresh_evidence = lambda: ran.append("refresh")
    env.state["now"] = at(11, 5, day=date(2026, 10, 10))
    env.engine.tick()
    assert ran == ["refresh"]
    env.engine.settings = dataclasses.replace(env.engine.settings, auto_backtest=False)
    env.state["now"] = at(11, 5, day=date(2026, 10, 17))
    env.engine.tick()
    assert ran == ["refresh"]
