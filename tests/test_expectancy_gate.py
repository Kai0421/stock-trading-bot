# Author: Kai0421
"""Tests for GATE_MODE=expectancy: the owner's alternative to the win-rate gate."""

import math
from datetime import date

import pandas as pd
import pytest

from bot.backtest import SignatureBook, Trade, acceptance, compute_metrics
from bot.config import ConfigError, RiskConfig, Settings
from bot.evidence import Evidence, EvidenceGate, evidence_from_trades, load_evidence, save_evidence
from bot.risk import mean_r_lower_bound, probability_gate, signature_disabled, size_position

WIN = RiskConfig()
EXP = RiskConfig(gate_mode="expectancy")


def test_gate_mode_from_env_and_validation():
    assert Settings.from_env({}).risk.gate_mode == "win_rate"
    assert Settings.from_env({"GATE_MODE": " Expectancy "}).risk.gate_mode == "expectancy"
    with pytest.raises(ConfigError, match="GATE_MODE"):
        Settings.from_env({"GATE_MODE": "yolo"})


def test_expectancy_mode_halves_risk_per_trade():
    assert WIN.effective_risk_per_trade == 0.01
    assert EXP.effective_risk_per_trade == 0.005
    assert (
        RiskConfig(gate_mode="expectancy", risk_per_trade=0.003).effective_risk_per_trade == 0.003
    )
    # $100k equity, $1 risk per share: 1000 shares in win-rate mode, 500 in expectancy mode.
    assert size_position(100_000, 50.0, 49.0, 1e9, WIN) == 400  # capped by 20% position size
    assert size_position(100_000, 10.0, 9.0, 1e9, WIN) == 1000
    assert size_position(100_000, 10.0, 9.0, 1e9, EXP) == 500


def test_mean_r_lower_bound():
    assert mean_r_lower_bound(0.1, 1.0, 400) == pytest.approx(0.1 - 1.959964 / 20, rel=1e-6)
    assert math.isnan(mean_r_lower_bound(0.1, math.nan, 400))


def test_expectancy_gate_ignores_win_rate_but_needs_a_positive_lower_bound():
    # 41% wins, mean +0.10R, sd 1.0, n=3529: lower bound ~ +0.067R -> passes.
    g = probability_gate(wins=1447, n=3529, expectancy_r=0.10, cfg=EXP, sd_r=1.0)
    assert g.passed
    assert not probability_gate(wins=1447, n=3529, expectancy_r=0.10, cfg=WIN, sd_r=1.0).passed
    # Same mean on only 120 trades: lower bound below zero -> refused.
    small = probability_gate(wins=50, n=120, expectancy_r=0.10, cfg=EXP, sd_r=1.0)
    assert not small.passed and "lower bound" in small.reason
    assert not probability_gate(wins=60, n=99, expectancy_r=0.5, cfg=EXP, sd_r=0.1).passed
    no_sd = probability_gate(wins=60, n=200, expectancy_r=0.5, cfg=EXP)
    assert not no_sd.passed and "re-run" in no_sd.reason


def trade(r, day, sig="s"):
    t = pd.Timestamp(day)
    return Trade("X", sig, t, t, t, 10.0, 9.0, 100, r * 100, r, "stop")


def test_signature_book_and_evidence_carry_the_r_spread():
    trades = [trade(r, f"2020-01-{d:02d}") for d, r in enumerate([2.0, -1.0, -1.0, 2.0], 1)]
    n, wins, mean, sd = SignatureBook(trades).stats("s", pd.Timestamp("2020-02-01"))
    assert (n, wins, mean) == (4, 2, 0.5)
    assert sd == pytest.approx(pd.Series([2.0, -1.0, -1.0, 2.0]).std())
    ev = evidence_from_trades(trades, pd.Timestamp("2020-02-01"))
    assert ev["s"].sd_r == pytest.approx(sd, abs=1e-6)


def test_old_evidence_files_load_without_sd(tmp_path):
    path = tmp_path / "e.json"
    path.write_text(
        '{"as_of": "2026-10-01", "backtest_id": "b", "verdict": "ACCEPTED", '
        '"signatures": {"s": {"n": 150, "wins": 120, "mean_r": 0.2}}}'
    )
    ev = load_evidence(path)
    assert ev.signatures["s"].sd_r is None
    gate = EvidenceGate(ev, {})
    assert gate.gate("s", pd.Timestamp("2026-10-02"), WIN).passed
    assert "re-run" in gate.gate("s", pd.Timestamp("2026-10-02"), EXP).reason
    save_evidence(path, ev)
    assert load_evidence(path).signatures["s"].sd_r is None


def test_live_disabling_compares_against_the_backtest_win_rate_in_expectancy_mode():
    ev = Evidence(
        signatures=evidence_from_trades(
            [trade(2.5 if i % 10 < 4 else -1.0, f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}")
             for i in range(200)],
            pd.Timestamp("2021-01-01"),
        ),
        as_of=date(2026, 10, 1), backtest_id="b", verdict="ACCEPTED", strategy="x",
    )  # fmt: skip
    as_of = pd.Timestamp("2026-10-02")
    on_track = [i % 10 < 4 for i in range(30)]  # 40% live, as in the backtest
    assert EvidenceGate(ev, {"s": on_track}).gate("s", as_of, EXP).passed
    assert signature_disabled(on_track, WIN)  # the win-rate floor would switch it off
    collapsed = [i % 10 < 2 for i in range(30)]  # 20% live: more than 15 points worse
    gate = EvidenceGate(ev, {"s": collapsed})
    assert not gate.gate("s", as_of, EXP).passed
    assert gate.disabled(EXP) == ["s"]


def test_acceptance_in_expectancy_mode():
    rs = [2.0 if i % 10 < 4 else -0.8 for i in range(400)]  # 40% wins, +0.32R
    trades = [trade(r, "2020-01-01") for r in rs]
    flat = pd.Series([100.0, 99.0, 103.0], index=pd.bdate_range("2020-01-01", periods=3))
    m = compute_metrics(trades, flat)
    assert m.expectancy_lb_r > 0
    assert acceptance(m, EXP) == []
    assert any("win rate" in f for f in acceptance(m, WIN))
    deep = pd.Series([100.0, 92.0, 120.0], index=flat.index)  # 8% drawdown
    assert any("drawdown" in f for f in acceptance(compute_metrics(trades, deep), EXP))
    assert not any("drawdown" in f for f in acceptance(compute_metrics(trades, deep), WIN))
