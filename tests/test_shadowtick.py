# Author: Kai0421
"""Shadow tick: what the decision logic would do for open positions, read-only."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from bot.broker import BrokerOrder, BrokerPosition
from bot.journal import Journal, PositionRow
from bot.shadowtick import (
    READ_METHODS,
    ReadOnlyBroker,
    Snapshot,
    WriteBlocked,
    compute_intents,
    diff_intents,
    flat_blockers,
    hot_blockers,
    snapshot,
    snapshot_from_json,
    snapshot_to_json,
)
from test_engine import FakeBroker

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 15, 11, 0, tzinfo=ET)
WRITES = [
    ("submit_entry", ("AAPL", 1, 100.0, 95.0, "c")),
    ("submit_stop", ("AAPL", 1, 95.0, "c")),
    ("sell_market", ("AAPL", 1, "c")),
    ("replace_stop", ("id", 95.0, 1)),
    ("cancel", ("id",)),
    ("submit_close_buy", ("AAPL", 1, 100.0, 95.0, "c")),
    ("sell_at_open", ("AAPL", 1, "c")),
]


def stop_order(id_, symbol, qty, price):
    return BrokerOrder(id_, f"{id_}-c", symbol, "sell", qty, 0, None, "stop", "simple", None,
                       price, "new", (), None, None)  # fmt: skip


def buy_order(id_, symbol, qty):
    return BrokerOrder(id_, f"{id_}-c", symbol, "buy", qty, 0, None, "limit", "oto", 100.0,
                       None, "new", (), None, None)  # fmt: skip


def row(symbol="MSFT", stop=96.0, order_id="s1", pending=None, qty=100):
    return PositionRow(symbol, 100.0, qty, qty, 96.0, stop, date(2026, 10, 14), 1, False, "sig",
                       0.8, "Information Technology", order_id, pending)  # fmt: skip


def pos(symbol="MSFT", qty=100, price=101.0):
    return BrokerPosition(symbol, qty, qty, 100.0, price, "us_equity")


# ------------------------------------------------------------------ read-only broker


@pytest.mark.parametrize("name, args", WRITES)
def test_every_broker_write_is_blocked(name, args):
    inner = FakeBroker()
    ro = ReadOnlyBroker(inner)
    with pytest.raises(WriteBlocked):
        getattr(ro, name)(*args)
    assert inner.calls == []  # the write never reached the broker


def test_unknown_methods_are_blocked_too():
    with pytest.raises(WriteBlocked):
        ReadOnlyBroker(FakeBroker()).anything_new()


def test_reads_pass_through():
    inner = FakeBroker()
    inner.pos["MSFT"] = pos()
    ro = ReadOnlyBroker(inner)
    assert set(ro.positions()) == {"MSFT"}
    assert ro.open_orders() == []
    assert {"positions", "open_orders", "account", "clock"} <= READ_METHODS


def test_snapshot_round_trips_through_json():
    inner = FakeBroker()
    inner.pos["MSFT"] = pos()
    inner.orders = [stop_order("s1", "MSFT", 100, 96.0)]
    snap = snapshot(ReadOnlyBroker(inner))
    back = snapshot_from_json(json.loads(json.dumps(snapshot_to_json(snap))))
    assert back == snap
    assert inner.calls == []


# ------------------------------------------------------------------ intents


def test_intents_are_the_decision_layers_answers_for_open_positions():
    positions = {"MSFT": pos(), "AAPL": pos("AAPL", 50)}
    orders = [stop_order("s1", "MSFT", 100, 94.0)]  # journal stop 96 > broker 94: raise it
    rows = {"MSFT": row(stop=96.0), "AAPL": row("AAPL", order_id=None, qty=50)}
    out = compute_intents(positions, orders, rows, NOW)
    assert [(s["symbol"], s["stop_price"]) for s in out["stop_changes"]] == [("MSFT", 96.0)]
    assert [(p["symbol"], p["qty"]) for p in out["protect"]] == [("AAPL", 50)]
    assert out["exits"] == []


def test_pending_exit_shows_up_as_an_exit_intent():
    out = compute_intents(
        {"MSFT": pos()},
        [stop_order("s1", "MSFT", 100, 96.0)],
        {"MSFT": row(pending=("profit_target", 100))},
        NOW,
    )
    assert [(e["symbol"], e["kind"], e["qty"]) for e in out["exits"]] == [
        ("MSFT", "profit_target", 100)
    ]


def test_intents_are_deterministic_and_json_safe():
    args = ({"MSFT": pos()}, [stop_order("s1", "MSFT", 100, 94.0)], {"MSFT": row()}, NOW)
    assert compute_intents(*args) == compute_intents(*args)
    json.dumps(compute_intents(*args))


def test_diff_reports_only_real_differences_in_plain_english():
    a = compute_intents(
        {"MSFT": pos()}, [stop_order("s1", "MSFT", 100, 94.0)], {"MSFT": row()}, NOW
    )
    assert diff_intents(a, a) == []
    b = compute_intents(
        {"MSFT": pos()}, [stop_order("s1", "MSFT", 100, 96.0)], {"MSFT": row()}, NOW
    )
    lines = diff_intents(a, b)
    assert lines and "MSFT" in lines[0] and "stop" in lines[0].lower()
    assert all("_" not in line for line in lines)


# ------------------------------------------------------------------ who may upgrade when


def test_flat_blockers():
    assert flat_blockers({}, []) == []
    assert flat_blockers({"MSFT": pos()}, []) != []
    assert flat_blockers({}, [buy_order("b1", "AAPL", 10)]) != []


def test_hot_path_allows_protected_positions_only(tmp_path):
    j = Journal(tmp_path / "bot.db")
    good = hot_blockers(
        {"MSFT": pos()}, [stop_order("s1", "MSFT", 100, 96.0)], {"MSFT": row()}, NOW
    )
    assert good == []
    # shares not fully covered by a stop
    part = hot_blockers({"MSFT": pos()}, [stop_order("s1", "MSFT", 40, 96.0)], {"MSFT": row()}, NOW)
    assert part and "MSFT" in part[0] and "stop" in part[0]
    # held at the broker but unknown to the journal
    assert hot_blockers({"ZZZ": pos("ZZZ")}, [stop_order("s9", "ZZZ", 100, 90.0)], {}, NOW)
    # an entry order is still working
    assert hot_blockers(
        {"MSFT": pos()},
        [stop_order("s1", "MSFT", 100, 96.0), buy_order("b1", "AAPL", 10)],
        {"MSFT": row()},
        NOW,
    )
    # journal says held but the broker has nothing: still being booked
    assert hot_blockers({}, [], {"MSFT": row()}, NOW)
    assert j is not None


# ------------------------------------------------------------------ the wrapper script


def test_wrapper_script_matches_the_in_process_answer(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).parents[1]
    j = Journal(tmp_path / "bot.db")
    j.upsert_position(row())
    j.db.commit()
    snap = Snapshot({"MSFT": pos()}, [stop_order("s1", "MSFT", 100, 94.0)])
    (tmp_path / "snap.json").write_text(json.dumps(snapshot_to_json(snap)))

    def run(*args):
        return subprocess.run(
            [sys.executable, str(repo / "scripts" / "shadow_tick.py"), *args],
            capture_output=True, text=True, cwd=repo,
        )  # fmt: skip

    done = run("--code", str(repo), "intents", "--db", str(tmp_path / "bot.db"),
               "--snapshot", str(tmp_path / "snap.json"), "--now", NOW.isoformat(),
               "--out", str(tmp_path / "out.json"))  # fmt: skip
    assert done.returncode == 0, done.stderr
    expected = compute_intents(snap.positions, snap.orders, j.positions(), NOW)
    assert json.loads((tmp_path / "out.json").read_text()) == expected
    same = run("diff", str(tmp_path / "out.json"), str(tmp_path / "out.json"))
    assert same.returncode == 0 and "exactly the same" in same.stdout
    (tmp_path / "other.json").write_text(
        json.dumps({**expected, "exits": [{"symbol": "MSFT", "kind": "x"}]})
    )
    differ = run("diff", str(tmp_path / "out.json"), str(tmp_path / "other.json"))
    assert differ.returncode == 1 and "MSFT" in differ.stdout
