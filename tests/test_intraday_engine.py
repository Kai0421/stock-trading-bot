# Author: Kai0421
"""Engine jobs in intraday mode (TRADING_MODE=intraday): schedule, scan and flatten."""

import dataclasses
from datetime import date, datetime

import pandas as pd
import pytest

from bot.broker import BrokerPosition
from bot.calendar import MarketCalendar
from bot.config import Settings
from bot.engine import Engine
from bot.evidence import Evidence, SignatureEvidence, save_evidence
from bot.intraday.strategies import OrbBreakout
from bot.journal import Journal, PositionRow
from bot.notify import Level
from test_engine import FakeBroker, FakeNotifier, at
from test_intraday import SPY, daily, orb_day

MON = date(2026, 10, 5)
UNIVERSE = {"AAPL": "Information Technology", "SPY": "Broad Market"}
SIG = OrbBreakout().prepare(orb_day(), daily(), SPY)["signature"].dropna().iloc[0]


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        dry_run=False, db_path=tmp_path / "bot.db", trading_mode="intraday",
        strategy_name="orb_breakout",
    )  # fmt: skip
    state = {"now": at(10, 12), "prices": {"AAPL": 101.5}}
    broker = FakeBroker()
    notifier = FakeNotifier()
    journal = Journal(settings.db_path)
    evidence_path = tmp_path / "evidence.json"
    save_evidence(
        evidence_path,
        Evidence(
            {SIG: SignatureEvidence(150, 120, 0.3, 0.8)},
            date(2026, 9, 30),
            "bt1",
            "ACCEPTED",
            "orb_breakout",
        ),  # fmt: skip
    )
    intraday = {"AAPL": orb_day(), "SPY": orb_day() * 5}

    def load_intraday(symbols, start, end):
        cut = pd.Timestamp(end.replace(tzinfo=None))
        return {s: df[df.index < cut] for s, df in intraday.items() if s in symbols}

    def load_bars(symbols, start, end):
        return {s: (SPY if s == "SPY" else daily()) for s in symbols}

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
        load_intraday=load_intraday,
    )
    return dataclasses.make_dataclass("Env", ["engine", "broker", "journal", "notifier", "state"])(
        engine, broker, journal, notifier, state
    )


def entries(broker):
    return [c for c in broker.calls if c[0] == "entry"]


def test_intraday_schedule_replaces_entries_and_manage(env):
    jobs = env.engine._schedule(MON)
    names = {j for j, _, _ in jobs}
    assert {"premarket", "scan", "flatten", "eod"} <= names
    assert "entries" not in names and "manage" not in names
    scans = [s for j, s, _ in jobs if j == "scan"]
    flat = [s for j, s, _ in jobs if j == "flatten"]
    assert scans[0].strftime("%H:%M") == "09:35" and scans[-1].strftime("%H:%M") == "15:45"
    assert flat[0].strftime("%H:%M") == "15:50"


def test_swing_schedule_is_unchanged(env):
    env.engine.settings = Settings(db_path=env.engine.settings.db_path)
    names = {j for j, _, _ in env.engine._schedule(MON)}
    assert {"entries", "manage"} <= names and "scan" not in names


def test_scan_enters_a_gated_setup_with_a_broker_stop(env):
    env.engine.scan()
    (entry,) = entries(env.broker)
    _, symbol, qty, limit, stop, cid = entry
    assert symbol == "AAPL" and cid == "2026-10-05-AAPL-ientry"
    assert 0 < stop < limit and qty > 0
    env.engine.scan()  # same bar again: no second entry today
    assert len(entries(env.broker)) == 1


def test_scan_without_evidence_does_not_enter(env):
    env.engine.evidence_path.unlink()
    env.engine.scan()
    assert entries(env.broker) == []


def test_scan_respects_the_pattern_day_trader_minimum(env):
    env.broker.equity = env.broker.cash = 20_000.0
    env.engine.scan()
    assert entries(env.broker) == []
    assert "No intraday trades: account too small" in env.notifier.titles(Level.WARN)


def test_swing_jobs_do_nothing_in_intraday_mode(env):
    env.state["now"] = at(9, 50)
    env.engine.entries()
    env.engine.manage()
    assert env.broker.calls == []


def test_flatten_sells_todays_positions_only(env):
    env.engine.scan()
    env.broker.fill_entry("AAPL", 101.5)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 5, 0, 300.0, 300.0, "us_equity")
    env.journal.upsert_position(
        PositionRow("MSFT", 300.0, 5, 5, 280.0, 280.0, date(2026, 9, 28), 5, False, "sig",
                    0.8, "Information Technology", None, None)
    )  # fmt: skip
    env.state["now"] = at(15, 51)
    env.engine.flatten()
    sells = [c for c in env.broker.calls if c[0] == "sell"]
    assert [(c[1], c[3]) for c in sells] == [("AAPL", "2026-10-05-AAPL-exit-flatten")]
    assert any("MSFT was left open" in t for t in env.notifier.titles(Level.CRITICAL))
    assert not [o for o in env.broker.orders if o.symbol == "AAPL" and o.type == "stop"]
    env.engine.flatten()  # the market sell is still working: no duplicate
    assert len([c for c in env.broker.calls if c[0] == "sell"]) == 1


def test_tick_runs_scan_in_intraday_mode(env):
    env.state["now"] = datetime(2026, 10, 5, 10, 12, tzinfo=env.state["now"].tzinfo)
    assert env.engine.tick() == ["scan"]
    assert len(entries(env.broker)) == 1
