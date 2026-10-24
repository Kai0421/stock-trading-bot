# Author: Kai0421
"""Golden trace of the engine's observable behaviour (docs/19-layers.md).

Runs scripted scenarios through the real ``Engine`` jobs (swing and intraday) with the
fake broker from ``test_engine`` and records everything the outside world can see:
every broker write (orders, stops, sells, replaces, cancels), the cash-lock check and
every account fetch, in order; every alert; the whole journal; and the memory files.

``tests/golden/engine_trace.json`` was written from main before the data / decision /
execution split; ``test_layer_equivalence`` re-runs the scenarios and requires the
same trace, so the refactor provably makes the same plans and orders.

Regenerate only on purpose (a reviewed behaviour change):
``uv run python tests/golden_engine.py --write``
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from bot.broker import BrokerError, BrokerPosition  # noqa: E402
from bot.calendar import MarketCalendar  # noqa: E402
from bot.config import Settings  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.evidence import Evidence, SignatureEvidence, save_evidence  # noqa: E402
from bot.journal import Journal, PlanRow, PositionRow  # noqa: E402
from test_engine import (  # noqa: E402
    AAPL_ATR,
    AAPL_SIG,
    FRI,
    MON,
    UNIVERSE,
    FakeBroker,
    FakeNotifier,
    at,
    market,
)

GOLDEN = Path(__file__).parent / "golden" / "engine_trace.json"
TUE = date(2026, 10, 6)


class TracingBroker(FakeBroker):
    """The fake broker, also logging account fetches (the fresh-cash-check rule)."""

    def account(self):
        self.calls.append(("account",))
        return super().account()


class DryRunBroker(TracingBroker):
    """Like ``bot.broker.Broker`` with ``DRY_RUN=true``: writes return None, send nothing."""

    def __init__(self):
        super().__init__()
        self.dry_run = True

    def submit_entry(self, symbol, qty, limit, stop, cid):
        self.calls.append(("entry", symbol, qty, limit, stop, cid))

    def submit_stop(self, symbol, qty, stop, cid):
        self.calls.append(("stop", symbol, qty, stop, cid))

    def sell_market(self, symbol, qty, cid):
        self.calls.append(("sell", symbol, qty, cid))


@dataclasses.dataclass
class Env:
    engine: Engine
    broker: TracingBroker
    journal: Journal
    notifier: FakeNotifier
    state: dict
    tmp: Path


def make_swing(tmp: Path, broker: TracingBroker | None = None, **settings) -> Env:
    s = Settings(dry_run=False, db_path=tmp / "bot.db", **settings)
    state = {"now": at(8, 30), "bars": market(), "prices": {"AAPL": 120.0, "MSFT": 110.0}}
    broker = broker or TracingBroker()
    notifier = FakeNotifier()
    journal = Journal(s.db_path)
    evidence_path = tmp / "evidence.json"
    save_evidence(
        evidence_path,
        Evidence(
            {AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "bt1", "ACCEPTED"
        ),
    )

    def load_bars(symbols, start, end):
        return {s: df for s, df in state["bars"].items() if s in symbols}

    engine = Engine(
        settings=s,
        broker=broker,
        journal=journal,
        notifier=notifier,
        calendar=MarketCalendar(),
        load_bars=load_bars,
        latest_prices=lambda syms: {k: p for k, p in state["prices"].items() if k in syms},
        clock=lambda: state["now"],
        universe=UNIVERSE,
        evidence_path=evidence_path,
        memory_dir=tmp / "memory",
        lock_path=tmp / "engine.lock",
        altdata=state.get("altdata"),
    )
    return Env(engine, broker, journal, notifier, state, tmp)


def make_intraday(tmp: Path, strategy_name: str = "orb_breakout", **settings) -> Env:
    from test_intraday import SPY, daily, orb_day
    from test_intraday_engine import SIG

    s = Settings(
        dry_run=False, db_path=tmp / "bot.db", trading_mode="intraday",
        strategy_name=strategy_name, **settings,
    )  # fmt: skip
    state = {"now": at(10, 12), "prices": {"AAPL": 101.5}}
    broker = TracingBroker()
    notifier = FakeNotifier()
    journal = Journal(s.db_path)
    evidence_path = tmp / "evidence.json"
    save_evidence(
        evidence_path,
        Evidence({SIG: SignatureEvidence(150, 120, 0.3, 0.8)}, date(2026, 9, 30), "bt1",
                 "ACCEPTED", "orb_breakout"),
    )  # fmt: skip
    intraday = {"AAPL": orb_day(), "SPY": orb_day() * 5}
    state["intraday"] = intraday

    def load_intraday(symbols, start, end):
        cut = pd.Timestamp(end.replace(tzinfo=None))
        return {k: df[df.index < cut] for k, df in state["intraday"].items() if k in symbols}

    def load_bars(symbols, start, end):
        return {k: (SPY if k == "SPY" else daily()) for k in symbols}

    engine = Engine(
        settings=s,
        broker=broker,
        journal=journal,
        notifier=notifier,
        calendar=MarketCalendar(),
        load_bars=load_bars,
        latest_prices=lambda syms: {k: p for k, p in state["prices"].items() if k in syms},
        clock=lambda: state["now"],
        universe={"AAPL": "Information Technology", "SPY": "Broad Market"},
        evidence_path=evidence_path,
        memory_dir=tmp / "memory",
        lock_path=tmp / "engine.lock",
        load_intraday=load_intraday,
    )
    return Env(engine, broker, journal, notifier, state, tmp)


# ------------------------------------------------------------------ helpers


def plan(symbol="AAPL", rank=1, sector="Information Technology"):
    return PlanRow(MON, rank, symbol, AAPL_SIG, 0.8, AAPL_ATR, 0.3, sector, 119.0)


def held(env, symbol, qty, entry, price, stop, pending=None, partial=False, day=FRI):
    env.broker.pos[symbol] = BrokerPosition(symbol, qty, 0, entry, price, "us_equity")
    order = FakeBroker.submit_stop(env.broker, symbol, qty, stop, f"s-{symbol}")
    env.journal.upsert_position(
        PositionRow(symbol, entry, qty, qty, stop, stop, day, 1, partial, AAPL_SIG, 0.8,
                    "Information Technology", order.id, pending)
    )  # fmt: skip
    return order


def run(env, job, h, m=0, day=MON):
    env.state["now"] = at(h, m, day=day)
    getattr(env.engine, job)()


def eod_setup(env, equity, prev=100_000.0, peak=100_000.0):
    env.journal.record_equity(date(2026, 9, 1), peak, peak)
    env.journal.record_equity(FRI, prev, prev)
    env.broker.equity = equity
    env.state["bars"] = market(end=MON)


# ------------------------------------------------------------------ swing scenarios


def swing_full_lifecycle(tmp):
    """Plan, enter, fill, adopt, cancel nothing, EOD trend break, exit next day, book."""
    env = make_swing(tmp)
    env.journal.record_equity(FRI, 100_000.0, 100_000.0)
    run(env, "premarket", 8, 30)
    run(env, "entries", 9, 50)
    run(env, "entries", 9, 55)  # idempotent
    env.broker.fill_entry("AAPL", 120.1)
    run(env, "manage", 10, 0)
    run(env, "manage", 11, 0)
    bars = market(end=MON)
    bars["AAPL"].iloc[-1, bars["AAPL"].columns.get_loc("close")] = 50.0
    bars["AAPL"].iloc[-1, bars["AAPL"].columns.get_loc("low")] = 49.0
    env.state["bars"] = bars
    run(env, "eod", 16, 20)
    env.state["bars"] = market(end=MON)
    run(env, "premarket", 8, 30, TUE)
    run(env, "entries", 9, 50, TUE)
    sell = next(o for o in env.broker.orders if o.side == "sell" and o.type == "market")
    env.broker.fill_sell(sell.id, 118.0)
    run(env, "manage", 10, 0, TUE)
    return env


def swing_stop_hit(tmp):
    env = make_swing(tmp)
    env.journal.save_plans(MON, [plan()])
    run(env, "entries", 9, 50)
    env.broker.fill_entry("AAPL", 120.0)
    run(env, "manage", 10, 0)
    row = env.journal.positions()["AAPL"]
    env.broker.fill_sell(row.stop_order_id, row.stop)
    run(env, "manage", 14, 30)
    return env


def swing_unfilled_entry_cancelled(tmp):
    env = make_swing(tmp)
    env.journal.save_plans(MON, [plan()])
    run(env, "entries", 9, 50)
    run(env, "manage", 10, 30)
    run(env, "manage", 11, 5)
    return env


def swing_batch_cash_and_sectors(tmp):
    env = make_swing(tmp)
    env.broker.cash = 25_000.0
    env.journal.save_plans(MON, [plan("AAPL", 1), plan("MSFT", 2)])
    env.state["prices"] = {"AAPL": 120.0, "MSFT": 120.0}
    run(env, "entries", 9, 50)
    return env


def swing_dry_run_batch(tmp):
    env = make_swing(tmp, DryRunBroker())
    env.engine.settings = dataclasses.replace(env.engine.settings, dry_run=True)
    env.broker.cash = 60_000.0
    env.journal.save_plans(MON, [plan("AAPL", 1), plan("MSFT", 2)])
    env.state["prices"] = {"AAPL": 120.0, "MSFT": 120.0}
    run(env, "premarket", 8, 30)
    env.journal.save_plans(MON, [plan("AAPL", 1), plan("MSFT", 2)])
    run(env, "entries", 9, 50)
    run(env, "manage", 10, 0)
    held(env, "SPY", 10, 100.0, 90.0, 95.0)
    env.broker.orders = []  # unprotected and below its stop: protect sells (dry run)
    run(env, "manage", 10, 30)
    return env


def swing_exits_full_partial_and_failed(tmp):
    env = make_swing(tmp)
    held(env, "MSFT", 100, 100.0, 105.0, 96.0, ("trend_break", 100))
    held(env, "AAPL", 100, 100.0, 109.0, 104.8, ("partial_profit", 50))
    run(env, "entries", 9, 50)
    env2 = make_swing(tmp / "b")
    held(env2, "MSFT", 100, 100.0, 109.0, 104.8, ("partial_profit", 50))
    held(env2, "AAPL", 100, 100.0, 105.0, 96.0, ("time_stop", 100))

    def boom(*a, **k):
        raise BrokerError("sell rejected")

    env2.broker.sell_market = boom
    run(env2, "entries", 9, 50)
    run(env2, "manage", 10, 0)
    return [env, env2]


def swing_partial_fill_then_full(tmp):
    env = make_swing(tmp)
    env.journal.save_plans(MON, [plan()])
    run(env, "entries", 9, 50)
    env.broker.fill_entry("AAPL", 120.0)
    run(env, "manage", 10, 0)
    row = env.journal.positions()["AAPL"]
    env.journal.upsert_position(dataclasses.replace(row, pending_exit=("partial_profit", 40)))
    run(env, "manage", 10, 30)
    sell = next(o for o in env.broker.orders if o.side == "sell" and o.type == "market")
    env.broker.fill_sell(sell.id, 126.0)
    run(env, "manage", 11, 0)
    env.journal.upsert_position(dataclasses.replace(env.journal.positions()["AAPL"], stop=121.5))
    run(env, "manage", 11, 30)
    stop = next(o for o in env.broker.orders if o.side == "sell" and o.type == "stop")
    env.broker.fill_sell(stop.id, 121.5)
    run(env, "manage", 12, 0)
    run(env, "eod", 16, 20)
    return env


def swing_unknown_and_unprotected(tmp):
    env = make_swing(tmp)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 10, 10, 100.0, 101.0, "us_equity")
    env.broker.pos["SPY"] = BrokerPosition("SPY", 5, 5, 100.0, 94.0, "us_equity")
    run(env, "manage", 10, 0)
    run(env, "manage", 10, 30)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 30, 30, 100.0, 101.0, "us_equity")
    run(env, "manage", 11, 0)  # broker holds more than the journal: critical, no booking
    return env


def swing_stop_raise_and_qty_trim(tmp):
    env = make_swing(tmp)
    stop = held(env, "MSFT", 100, 100.0, 108.0, 96.0)
    row = env.journal.positions()["MSFT"]
    env.journal.upsert_position(dataclasses.replace(row, stop=100.0))
    run(env, "manage", 10, 0)
    run(env, "manage", 10, 30)
    env.broker.orders = [dataclasses.replace(o, qty=150) if o.id == stop.id else o
                         for o in env.broker.orders]  # fmt: skip
    run(env, "manage", 11, 0)
    return env


def swing_blocks(tmp):
    envs = []
    for i, setup in enumerate(("lock", "account", "paused", "halt", "window", "late", "closed")):
        env = make_swing(tmp / str(i))
        env.journal.save_plans(MON, [plan()])
        h, m = 9, 50
        if setup == "lock":
            env.broker.lock_ok = False
        elif setup == "account":
            env.broker.fail_account = True
        elif setup == "paused":
            env.journal.set_flag("paused", True, "test", at(8))
        elif setup == "halt":
            env.journal.set_flag("halt_today", True, "test", at(8))
            env.journal.set_kv("halt_day", MON.isoformat())
        elif setup == "window":
            h, m = 9, 35
        elif setup == "late":
            h, m = 11, 30
        elif setup == "closed":
            env.broker.is_open = False
        run(env, "entries", h, m)
        envs.append(env)
    return envs


def max_positions(env, n):
    s = env.engine.settings
    env.engine.settings = dataclasses.replace(
        s, risk=dataclasses.replace(s.risk, max_open_positions=n)
    )


def two_setups_one_slot(env):
    """AAPL and MSFT both set up; one free slot goes to the higher win rate."""
    from bot.backtest import prepare
    from bot.config import StrategyConfig
    from test_engine import ohlcv, trend

    c = trend(296, 100.0, 0.005)
    c += [c[-1] * 0.985**k for k in (1, 2, 3)]
    env.state["bars"]["MSFT"] = ohlcv([*c, c[-1] * 1.03])  # a different signature
    _, tables = prepare(env.state["bars"], StrategyConfig())
    msft = tables["MSFT"]["signature"].iloc[-1]
    sigs = {AAPL_SIG: SignatureEvidence(150, 120, 0.3), msft: SignatureEvidence(150, 125, 0.3)}
    save_evidence(env.engine.evidence_path, Evidence(sigs, date(2026, 9, 30), "b", "ACCEPTED"))
    max_positions(env, 3)
    for s in ("X", "Y"):
        held(env, s, 1, 10.0, 11.0, 9.0)


def swing_premarket_variants(tmp):
    envs = []
    variants: list[tuple[str, Callable]] = [
        ("strict", lambda e: setattr(e.engine, "settings", dataclasses.replace(
            e.engine.settings, earnings_policy="strict"))),
        ("no_evidence", lambda e: e.engine.evidence_path.unlink()),
        ("rejected", lambda e: save_evidence(e.engine.evidence_path, Evidence(
            {AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", "REJECTED"))),
        ("trial", lambda e: (save_evidence(e.engine.evidence_path, Evidence(
            {AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", "REJECTED")),
            setattr(e.engine, "settings", dataclasses.replace(
                e.engine.settings, paper_trial=True)))),
        ("other_strategy", lambda e: setattr(e.engine, "settings", dataclasses.replace(
            e.engine.settings, strategy_name="rsi2_reversion"))),
        ("stale", lambda e: e.state.update(bars=market(end=date(2026, 10, 1)))),
        ("paused", lambda e: e.journal.set_flag("paused", True, "drawdown", at(8))),
        ("held", lambda e: held(e, "AAPL", 10, 100.0, 120.0, 95.0)),
        ("one_slot_left", lambda e: (max_positions(e, 3),
            [held(e, s, 1, 10.0, 11.0, 9.0) for s in ("X", "Y")])),
        ("two_setups_one_slot", two_setups_one_slot),
        ("slots_full", lambda e: (max_positions(e, 3),
            [held(e, s, 1, 10.0, 11.0, 9.0) for s in ("X", "Y", "Z")])),
        ("expectancy_gate", lambda e: setattr(e.engine, "settings", dataclasses.replace(
            e.engine.settings, risk=dataclasses.replace(
                e.engine.settings.risk, gate_mode="expectancy")))),
        ("altdata_veto", lambda e: (setattr(e.engine, "settings", dataclasses.replace(
            e.engine.settings, altdata_features=frozenset({"news_shock_veto"}))),
            setattr(e.engine, "altdata", lambda s, d: {"news_shock_veto": s == "AAPL"}))),
        ("altdata_pass", lambda e: (setattr(e.engine, "settings", dataclasses.replace(
            e.engine.settings, altdata_features=frozenset({"news_shock_veto"}))),
            setattr(e.engine, "altdata", lambda s, d: {"news_shock_veto": False}))),
        ("early_close", lambda e: None),
        ("saturday", lambda e: None),
        ("dry_run", lambda e: setattr(e.engine, "settings", dataclasses.replace(
            e.engine.settings, dry_run=True))),
    ]  # fmt: skip
    for name, setup in variants:
        env = make_swing(tmp / name)
        setup(env)
        if name == "early_close":
            nov27 = date(2026, 11, 27)
            env.state["bars"] = market(end=date(2026, 11, 25))
            run(env, "premarket", 8, 30, nov27)
        elif name == "saturday":
            run(env, "premarket", 8, 30, date(2026, 10, 3))
        else:
            run(env, "premarket", 8, 30)
        envs.append(env)
    return envs


def swing_eod_variants(tmp):
    envs = []
    for name, eq, prev, peak in (
        ("flat", 100_500.0, 100_000.0, 100_000.0),
        ("daily_loss", 96_000.0, 100_000.0, 100_000.0),
        ("drawdown", 89_000.0, 90_000.0, 100_000.0),
    ):
        env = make_swing(tmp / name)
        eod_setup(env, eq, prev, peak)
        held(env, "MSFT", 10, 100.0, 105.0, 96.0)
        if name == "flat":
            env.broker.cash = -5.0
        run(env, "eod", 16, 20)
        envs.append(env)
    env = make_swing(tmp / "missing_bar")
    eod_setup(env, 100_000.0)
    held(env, "AAPL", 10, 100.0, 120.0, 95.0)
    env.state["bars"] = market()  # last bar Friday: no bar for today
    env.engine.settings = dataclasses.replace(env.engine.settings, routine_daily_url="https://x")
    env.engine.review_exists = lambda day: False
    env.journal.set_kv(f"review_fired:{FRI}", "1")
    run(env, "eod", 16, 20)
    envs.append(env)
    return envs


def swing_tick_day(tmp):
    env = make_swing(tmp)
    env.journal.record_equity(FRI, 100_000.0, 100_000.0)
    for h, m in ((8, 35), (8, 40), (9, 47), (10, 2), (10, 33), (11, 1), (16, 16), (17, 55)):
        env.state["now"] = at(h, m)
        env.engine.tick()
    return env


# ------------------------------------------------------------------ intraday scenarios


def intraday_scan_and_flatten(tmp):
    env = make_intraday(tmp)
    run(env, "premarket", 8, 30)
    run(env, "scan", 10, 12)
    env.broker.fill_entry("AAPL", 101.5)
    run(env, "scan", 10, 17)
    run(env, "scan", 10, 22)
    run(env, "scan", 11, 0)
    env.broker.pos["MSFT"] = BrokerPosition("MSFT", 5, 0, 300.0, 300.0, "us_equity")
    env.journal.upsert_position(
        PositionRow("MSFT", 300.0, 5, 5, 280.0, 280.0, date(2026, 9, 28), 5, False, "sig",
                    0.8, "Information Technology", None, None)
    )  # fmt: skip
    run(env, "flatten", 15, 51)
    run(env, "flatten", 15, 56)
    env.journal.record_equity(FRI, 100_000.0, 100_000.0)
    run(env, "eod", 16, 20)
    return env


def intraday_stale_entry_cancel(tmp):
    env = make_intraday(tmp)
    run(env, "scan", 10, 12)
    run(env, "scan", 10, 25)  # unfilled for 10+ minutes: cancelled
    run(env, "flatten", 15, 51)
    return env


def intraday_exit_and_stop_raise(tmp):
    env = make_intraday(tmp)
    run(env, "scan", 10, 12)
    env.broker.fill_entry("AAPL", 101.5)
    for h, m in ((10, 22), (10, 47), (11, 32), (12, 7), (13, 2), (14, 7), (15, 2)):
        run(env, "scan", h, m)
    env2 = make_intraday(tmp / "b")
    run(env2, "scan", 10, 12)
    env2.broker.fill_entry("AAPL", 101.5)
    closes = env2.state["intraday"]["AAPL"].copy()
    closes.loc[closes.index[-60] :, ["open", "high", "low", "close"]] = 110.0
    env2.state["intraday"]["AAPL"] = closes
    run(env2, "scan", 11, 7)
    run(env2, "scan", 11, 12)
    return [env, env2, intraday_gap_fill_exit(tmp / "c")]


def intraday_gap_fill_exit(tmp):
    """Gap fill: enter on the reclaim, exit at the previous close (a strategy exit)."""
    from bot.intraday.strategies import GapFill
    from test_intraday import SPY, daily, gap_day

    sig = GapFill().prepare(gap_day(), daily(), SPY)["signature"].dropna().iloc[0]
    env = make_intraday(tmp, strategy_name="gap_fill")
    save_evidence(
        env.engine.evidence_path,
        Evidence({sig: SignatureEvidence(150, 120, 0.3, 0.8)}, date(2026, 9, 30), "bt1",
                 "ACCEPTED", "gap_fill"),
    )  # fmt: skip
    env.state["intraday"] = {"AAPL": gap_day(), "SPY": gap_day() * 5}
    env.state["prices"] = {"AAPL": 99.4}
    for h, m in ((9, 57), (10, 2), (10, 7), (10, 12)):
        run(env, "scan", h, m)
        if h == 9:
            env.broker.fill_entry("AAPL", 99.4)
    return env


def intraday_blocks(tmp):
    envs = []
    for name in ("pdt", "no_evidence", "kill", "lock", "paused", "late", "swing_jobs"):
        env = make_intraday(tmp / name)
        if name == "pdt":
            env.broker.equity = env.broker.cash = 20_000.0
        elif name == "no_evidence":
            env.engine.evidence_path.unlink()
        elif name == "kill":
            env.journal.record_equity(FRI, 120_000.0, 120_000.0)
        elif name == "lock":
            env.broker.lock_ok = False
        elif name == "paused":
            env.journal.set_flag("paused", True, "drawdown", at(8))
        if name == "late":
            run(env, "scan", 15, 47)
        elif name == "swing_jobs":
            run(env, "entries", 9, 50)
            run(env, "manage", 10, 0)
        else:
            run(env, "scan", 10, 12)
            run(env, "scan", 10, 17)
        envs.append(env)
    return envs


SCENARIOS: dict[str, Callable] = {
    f.__name__: f
    for f in (
        swing_full_lifecycle,
        swing_stop_hit,
        swing_unfilled_entry_cancelled,
        swing_batch_cash_and_sectors,
        swing_dry_run_batch,
        swing_exits_full_partial_and_failed,
        swing_partial_fill_then_full,
        swing_unknown_and_unprotected,
        swing_stop_raise_and_qty_trim,
        swing_blocks,
        swing_premarket_variants,
        swing_eod_variants,
        swing_tick_day,
        intraday_scan_and_flatten,
        intraday_stale_entry_cancel,
        intraday_exit_and_stop_raise,
        intraday_blocks,
    )
}


# ------------------------------------------------------------------ trace


def _journal_dump(path: Path) -> dict:
    con = sqlite3.connect(path)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {}
    for t in sorted(tables):
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
        rows = con.execute(f"SELECT * FROM {t}").fetchall()
        out[t] = sorted([dict(zip(cols, r, strict=True)) for r in rows], key=json.dumps)
    con.close()
    return out


def _memory_dump(root: Path) -> dict:
    if not root.exists():
        return {}
    files = (p for p in sorted(root.rglob("*")) if p.is_file())
    return {p.relative_to(root).as_posix(): p.read_text() for p in files}


def trace(env: Env) -> dict:
    return {
        "broker": [list(c) for c in env.broker.calls],
        "alerts": [[lvl.name, title, text] for lvl, title, text in env.notifier.sent],
        "journal": _journal_dump(env.engine.settings.db_path),
        "memory": _memory_dump(env.engine.memory_dir),
    }


def run_scenario(name: str, tmp: Path) -> list[dict]:
    tmp.mkdir(parents=True, exist_ok=True)
    out = SCENARIOS[name](tmp)
    envs = out if isinstance(out, list) else [out]
    data = json.loads(json.dumps([trace(e) for e in envs], default=str))
    return _mask_tmp(data, str(tmp))


def _mask_tmp(obj, tmp: str):
    """Replace the temp dir with <tmp> and use / after it, so traces match on Windows too."""
    if isinstance(obj, str):
        if tmp not in obj:
            return obj
        head, *rest = obj.split(tmp)
        return head + "".join("<tmp>" + re.sub(r"\\(?=\S)", "/", r) for r in rest)
    if isinstance(obj, list):
        return [_mask_tmp(v, tmp) for v in obj]
    if isinstance(obj, dict):
        return {_mask_tmp(k, tmp): _mask_tmp(v, tmp) for k, v in obj.items()}
    return obj


def run_all(tmp: Path) -> dict[str, list[dict]]:
    return {name: run_scenario(name, tmp / name) for name in SCENARIOS}


if __name__ == "__main__":
    import tempfile

    if "--write" not in sys.argv:
        sys.exit("usage: python tests/golden_engine.py --write")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        result = run_all(Path(d))
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN} ({len(result)} scenarios)")
