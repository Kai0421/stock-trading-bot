# Author: Kai0421
"""Layer boundaries (docs/19-layers.md) and the decision layer tested without any fakes.

- decision (bot.decide) is pure: it never imports the broker client, the journal, the
  data loaders or the notifier;
- data (bot.gather) only reads;
- execution (bot.execute) is the only module outside bot.broker that sends orders.
"""

import ast
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from bot import decide
from bot.broker import AccountSnapshot, BrokerOrder, BrokerPosition
from bot.config import RiskConfig
from bot.evidence import Evidence, EvidenceGate, SignatureEvidence
from bot.journal import PositionRow
from bot.risk import Account
from bot.strategies import get_strategy
from test_engine import AAPL_SIG, BARS, FEATURES, FRI, MON, TABLES, UNIVERSE

SRC = Path(__file__).parents[1] / "src" / "bot"
ET = ZoneInfo("America/New_York")
BROKER_WRITES = {
    "submit_entry", "submit_stop", "sell_market", "replace_stop", "cancel", "submit_close_buy",
    "sell_at_open",
}  # fmt: skip
JOURNAL_WRITES = {
    "record_order", "record_signal", "record_trade", "record_equity", "upsert_position",
    "delete_position", "save_plans", "set_flag", "set_kv", "add_unsettled", "once",
}  # fmt: skip


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
    return out


def _calls(path: Path, on: str | None = None) -> set[str]:
    """Method names called in ``path`` (only on objects named ``on``, if given)."""

    def receiver(f: ast.Attribute) -> str | None:
        v = f.value
        return v.attr if isinstance(v, ast.Attribute) else getattr(v, "id", None)

    return {
        n.func.attr
        for n in ast.walk(ast.parse(path.read_text()))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and (on is None or receiver(n.func) == on)
    }


# ------------------------------------------------------------------ boundaries


def test_decision_layer_imports_no_io():
    imports = _imports(SRC / "decide.py")
    assert not imports & {"bot.broker.Broker", "bot.journal.Journal"}
    io = ("bot.notify", "bot.data", "bot.gather", "bot.execute", "bot.altdata", "subprocess")
    assert not {i for i in imports if i.startswith(io)}
    assert not _calls(SRC / "decide.py") & (BROKER_WRITES | JOURNAL_WRITES)


def test_data_layer_only_reads():
    assert not _calls(SRC / "gather.py") & (BROKER_WRITES | JOURNAL_WRITES)


def test_only_the_execution_layer_sends_orders():
    senders = {
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if _calls(p, on="broker") & BROKER_WRITES
    }
    assert senders == {"execute.py"}


# ------------------------------------------------------------------ pure decisions


def gate(verdict="ACCEPTED"):
    ev = Evidence({AAPL_SIG: SignatureEvidence(150, 120, 0.3)}, date(2026, 9, 30), "b", verdict)
    return EvidenceGate(ev, {})


def plan(**kw):
    args = dict(
        today=MON, prev=FRI, strategy=get_strategy("trend_pullback"), features=FEATURES,
        tables=TABLES, held=set(), open_slots=3, gate=gate(), risk=RiskConfig(),
        sectors=UNIVERSE, earnings_policy="flag", paper_trial=False, altdata=None,
        altdata_features=frozenset(),
    )  # fmt: skip
    return decide.plan_entries(**{**args, **kw})


def test_plan_entries_is_a_pure_function_of_snapshots():
    out = plan()
    assert [p.symbol for p in out.plans] == ["AAPL"]
    assert out.plans[0].win_prob == pytest.approx(0.8)
    (sig,) = out.signals
    assert sig.taken and sig.reason == "planned"
    assert plan() == out  # same inputs, same plan


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"held": {"AAPL"}}, "already held or ordered"),
        ({"open_slots": 0}, "no slots"),
        ({"earnings_policy": "strict"}, "earnings date unknown (policy=strict)"),
        ({"gate": gate("REJECTED")}, "strategy backtest not accepted (REJECTED)"),
        (
            {"altdata": {"AAPL": {"news_shock_veto": True}},
             "altdata_features": frozenset({"news_shock_veto"})},
            "alt-data veto: news_shock_veto",
        ),
    ],
)  # fmt: skip
def test_plan_entries_rejections(kw, reason):
    out = plan(**kw)
    assert out.plans == [] and [s.reason for s in out.signals] == [reason]


def test_premarket_blocker_reasons():
    spy = BARS["SPY"]
    ev = gate().evidence
    assert decide.premarket_blocker(False, spy, FRI, ev, "p", "trend_pullback", False) is None
    note, alert = decide.premarket_blocker(False, spy, MON, ev, "p", "trend_pullback", False)
    assert "stale" in note and alert.level == "WARN"
    note, _ = decide.premarket_blocker(False, spy, FRI, None, "p", "trend_pullback", False)
    assert note == "no backtest evidence"
    assert decide.premarket_blocker(True, None, FRI, None, "p", "x", False)[1] is None


def test_premarket_blocker_refuses_evidence_from_another_universe():
    spy = BARS["SPY"]
    ev = gate().evidence  # backtested on sp100
    note, alert = decide.premarket_blocker(
        False, spy, FRI, ev, "p", "trend_pullback", False, "sp500"
    )
    assert "universe sp100, not sp500" in note and alert.level == "WARN"


def snap(cash=100_000.0):
    return AccountSnapshot(Account(cash, cash, 100_000.0), False, False, "active")


def test_size_entry_respects_tradable_cash_and_dry_run_commitments():
    common = dict(
        symbol="AAPL", limit=120.0, stop=115.0, sector="Information Technology",
        cid="c", win_prob=0.8, open_orders=[], positions=[], sector_of=UNIVERSE,
        unsettled=0.0, committed_sectors=[], risk=RiskConfig(),
    )  # fmt: skip
    order, _ = decide.size_entry(snap=snap(), committed_cost=[], **common)
    assert order is not None and order.shares * order.limit <= 0.20 * 100_000
    order, decision = decide.size_entry(snap=snap(5_000.0), committed_cost=[2_900.0], **common)
    assert order is None and decision.reason == "not enough tradable cash"


def stop_order(id_, symbol, qty, price, status="new"):
    return BrokerOrder(id_, f"{id_}-c", symbol, "sell", qty, 0, None, "stop", "simple", None,
                       price, status, (), None, None)  # fmt: skip


def row(symbol="MSFT", stop=96.0, pending=None, order_id="s1"):
    return PositionRow(symbol, 100.0, 100, 100, 96.0, stop, FRI, 1, False, AAPL_SIG, 0.8,
                       "Information Technology", order_id, pending)  # fmt: skip


def test_protect_covers_unprotected_shares_or_sells_below_the_stop():
    now = datetime(2026, 10, 5, 10, 0, tzinfo=ET)
    pos = {
        "MSFT": BrokerPosition("MSFT", 100, 100, 100.0, 101.0, "us_equity"),
        "SPY": BrokerPosition("SPY", 10, 10, 100.0, 94.0, "us_equity"),
    }
    acts = decide.protect_actions(pos, [stop_order("s1", "MSFT", 60, 96.0)], {"MSFT": row()}, now)
    assert [(a.symbol, a.qty, a.stop, a.sell) for a in acts] == [
        ("MSFT", 40, 96.0, False),
        ("SPY", 10, 95.0, True),
    ]


def test_stop_changes_only_raise_or_trim():
    pos = {"MSFT": BrokerPosition("MSFT", 50, 50, 100.0, 108.0, "us_equity")}
    orders = [stop_order("s1", "MSFT", 100, 96.0)]
    assert decide.stop_changes({"MSFT": row(stop=100.0)}, orders, pos) == [
        decide.StopChange("s1", "MSFT", stop_price=100.0)
    ]
    assert decide.stop_changes({"MSFT": row(stop=90.0)}, orders, pos) == [
        decide.StopChange("s1", "MSFT", qty=50)
    ]


def test_exit_intents_full_and_partial():
    pos = {"MSFT": BrokerPosition("MSFT", 100, 100, 100.0, 108.0, "us_equity")}
    (full,) = decide.exit_intents({"MSFT": row(pending=("trend_break", 100))}, pos, MON)
    assert (full.full, full.qty, full.cid) == (True, 100, "2026-10-05-MSFT-exit-trend_break")
    (part,) = decide.exit_intents({"MSFT": row(pending=("partial_profit", 50))}, pos, MON)
    assert (part.full, part.qty, part.cid) == (False, 50, "2026-10-05-MSFT-partial")


def test_manage_positions_uses_the_pluggable_strategy():
    bars = {"AAPL": BARS["AAPL"]}
    r = row("AAPL", stop=40.0, order_id=None)
    for name in ("trend_pullback", "rsi2_reversion", "confluence_reversion"):
        updated, anomalies = decide.manage_positions(
            {"AAPL": r}, bars, get_strategy(name), FRI, {"AAPL": 1}
        )
        assert anomalies == [] and updated[0].stop >= r.stop  # stops never move down
    _, anomalies = decide.manage_positions(
        {"AAPL": r}, bars, get_strategy("trend_pullback"), MON, {"AAPL": 1}
    )
    assert anomalies == ["AAPL: no bar for 2026-10-05; stop/exit not updated"]


def test_features_frames_are_not_mutated_by_planning():
    before = {s: t.copy() for s, t in TABLES.items()}
    plan()
    for s, t in TABLES.items():
        pd.testing.assert_frame_equal(t, before[s])
