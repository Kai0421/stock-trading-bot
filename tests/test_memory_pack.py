# Author: Kai0421
"""Tests for bot.memory (state.json, session context) and bot.pack (size-capped packs)."""

import json
from datetime import UTC, date, datetime

import pytest

from bot.journal import PositionRow
from bot.memory import build_state, session_context, write_state
from bot.pack import (
    DAILY_BUDGET,
    WEEKLY_BUDGET,
    Section,
    cleanup_packs,
    daily_pack,
    exit_code,
    render,
    short_signature,
    weekly_pack,
    write_pack,
)


def position(symbol="MSFT", **kw):
    base = dict(
        symbol=symbol,
        entry_price=420.1,
        shares=50,
        shares_initial=50,
        initial_stop=405.3,
        stop=405.3,
        entry_date=date(2026, 10, 1),
        days_held=4,
        partial_taken=False,
        signature="up|mid|strong|deep|near",
        win_prob=0.74,
        sector="Information Technology",
        stop_order_id="s1",
        pending_exit=None,
    )
    base.update(kw)
    return PositionRow(**base)


# ------------------------------------------------------- state.json


def test_build_state_matches_documented_shape():
    as_of = datetime(2026, 10, 5, 20, 15, tzinfo=UTC)
    state = build_state(
        as_of=as_of,
        equity=100_000.0,
        peak_equity=105_000.0,
        flags={"kill_switch": False, "paused": False},
        positions=[position()],
        prices={"MSFT": 429.0},
        last_runs={"premarket": "ok", "eod": "ok"},
    )
    assert state["mode"] == "paper"
    assert state["drawdown_pct"] == pytest.approx(4.76, abs=0.01)
    assert state["strategy_version"] == "trend-pullback-v1"
    (p,) = state["open_positions"]
    assert p == {
        "symbol": "MSFT",
        "shares": 50,
        "entry": 420.1,
        "stop": 405.3,
        "r_multiple": pytest.approx(0.6, abs=0.01),
        "days_held": 4,
    }
    assert state["as_of"] == as_of.isoformat()


def test_write_state_is_atomic_json(tmp_path):
    path = tmp_path / "memory" / "state.json"
    write_state(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert not list(path.parent.glob("*.tmp"))


def test_session_context_is_small_and_handles_missing_files(tmp_path):
    assert "no state" in session_context(tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps({"equity": 100000.0, "drawdown_pct": 1.2, "flags": {"paused": False},
                    "open_positions": [{"symbol": "MSFT"}], "as_of": "x"})
    )  # fmt: skip
    (tmp_path / "lessons.md").write_text(
        "# Lessons\n" + "".join(f"- lesson {i}\n" for i in range(60))
    )
    ctx = session_context(tmp_path, max_lessons=5)
    assert "equity 100000" in ctx and "MSFT" in ctx
    assert "lesson 59" in ctx and "lesson 54" not in ctx
    assert len(ctx) < 2000


# ------------------------------------------------------- pack helpers


def test_short_signature_and_exit_codes():
    assert short_signature("up|mid|strong|deep|near") == "TP-UMSDN"
    assert short_signature("up|high|weak|shallow|far") == "TP-UHWSF"
    assert short_signature("") == "TP-?"
    assert exit_code("stop") == "STOP"
    assert exit_code("trail") == "TRAIL"
    assert exit_code("time_stop") == "TIME"
    assert exit_code("trend_break") == "TREND"
    assert exit_code("partial_profit") == "TGT"
    assert exit_code("weird") == "OTHER"


def test_render_cuts_lowest_priority_first():
    sections = [
        Section("Keep", ["important"] * 3, priority=10),
        Section("Cut me", [f"row {i} " + "x" * 50 for i in range(200)], priority=1),
    ]
    text = render("# Pack", sections, budget=1_000)
    assert len(text) <= 1_000
    assert "important" in text
    assert "[truncated:" in text


def test_render_drops_whole_sections_if_needed():
    sections = [Section("Big", ["y" * 900], priority=1), Section("Small", ["ok"], priority=5)]
    text = render("# P", sections, budget=200)
    assert len(text) <= 200 and "ok" in text


# ------------------------------------------------------- daily / weekly packs


def big_daily(n):
    return daily_pack(
        day=date(2026, 10, 5),
        account={"equity": 100123.456, "day_pnl": -123.4, "day_pct": -0.00123, "drawdown": 0.012},
        fills=[{"symbol": f"S{i}", "side": "buy", "qty": 10, "price": 100.123} for i in range(n)],
        exits=[
            {"symbol": f"E{i}", "qty": 5, "price": 99.5, "pnl": -10.0, "r": -0.5, "reason": "stop"}
            for i in range(n)
        ],
        positions=[position(f"P{i}") for i in range(n)],
        prices={f"P{i}": 425.0 for i in range(n)},
        rejections={"probability gate": 12, "no slots": 3},
        gate_health=[{"signature": "up|mid|strong|deep|near", "n": 31, "win_rate": 0.7}] * n,
        flags={"paused": False, "kill_switch": False},
        anomalies=["stop gapped through on X"],
    )


def test_daily_pack_content_and_rounding():
    text = big_daily(2)
    assert text.startswith("# Daily pack 2026-10-05")
    assert "100,123" in text and "100123.456" not in text
    assert "TP-UMSDN" in text
    assert "STOP" in text
    assert "probability gate: 12" in text
    assert "stop gapped through" in text


def test_daily_pack_budget_enforced():
    assert len(big_daily(500)) <= DAILY_BUDGET


def test_weekly_pack_budget_enforced():
    trades = [
        {"symbol": f"S{i}", "entry_date": "2026-10-01", "exit_date": "2026-10-02", "pnl": float(i),
         "r_multiple": 0.1 * i, "exit_reason": "trail", "signature": "up|mid|strong|deep|near",
         "holding_days": 1}
        for i in range(2_000)
    ]  # fmt: skip
    text = weekly_pack(
        week="2026-W41",
        metrics={
            "week": {"trades": 5, "win_rate": 0.8, "expectancy_r": 0.3},
            "all": {"trades": 50, "win_rate": 0.72, "expectancy_r": None},
        },
        trades=trades,
        signature_health=[{"signature": "up|mid|strong|deep|near", "n": 40, "win_rate": 0.7}],
        altdata_hits=[],
        rule_audit={"blocked": 3, "reached_broker": 0},
        proposals=["raise RSI bucket edge (pending backtest)"],
    )
    assert text.startswith("# Weekly pack 2026-W41")
    assert len(text) <= WEEKLY_BUDGET
    assert "reached_broker: 0" in text
    assert "| expectancy_r | 0.30 | n/a |" in text  # R, not a percentage
    assert "| win_rate | 80.0% | 72.0% |" in text


def test_write_and_cleanup_packs(tmp_path):
    write_pack(tmp_path, "daily-2026-08-01.md", "old")
    write_pack(tmp_path, "daily-2026-10-01.md", "new")
    write_pack(tmp_path, "weekly-2026-W30.md", "old weekly")
    (tmp_path / "notes.txt").write_text("keep")
    removed = cleanup_packs(tmp_path, today=date(2026, 10, 5), keep_days=30)
    assert sorted(p.name for p in removed) == ["daily-2026-08-01.md", "weekly-2026-W30.md"]
    assert (tmp_path / "daily-2026-10-01.md").exists() and (tmp_path / "notes.txt").exists()


def test_profit_target_exit_code():
    assert exit_code("profit_target") == "TGT"
