# Author: Kai0421
"""Tests for bot.evidence: backtest evidence file and the live probability gate."""

import dataclasses
from datetime import date

import pandas as pd
import pytest

from bot.backtest import Trade
from bot.config import RiskConfig
from bot.evidence import (
    Evidence,
    EvidenceGate,
    SignatureEvidence,
    evidence_file,
    evidence_from_trades,
    load_evidence,
    save_evidence,
)

RISK = RiskConfig()


def trade(exit_day, win=True, sig="A", reason="stop"):
    d = pd.Timestamp(exit_day)
    pnl = 10.0 if win else -10.0
    return Trade("X", sig, d, d, d, 100.0, 95.0, 1, pnl, pnl / 5, reason)


def test_evidence_counts_only_closed_trades_before_as_of():
    days = pd.bdate_range("2020-01-01", periods=10)
    trades = [trade(d, win=i % 2 == 0) for i, d in enumerate(days)]
    trades.append(trade(days[2], sig="B", reason="end_of_data"))
    ev = evidence_from_trades(trades, as_of=days[6])
    assert ev.keys() == {"A"}
    assert (ev["A"].n, ev["A"].wins, ev["A"].mean_r) == (6, 3, 0.0)
    assert ev["A"].sd_r == pytest.approx(2.19089, abs=1e-4)


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "evidence" / "signatures.json"
    ev = Evidence(
        signatures={"A": SignatureEvidence(150, 120, 0.3)},
        as_of=date(2026, 9, 30),
        backtest_id="20261004-abcd1234",
        verdict="ACCEPTED",
    )
    save_evidence(path, ev)
    assert load_evidence(path) == ev
    assert load_evidence(path).universe == "sp100"
    assert load_evidence(tmp_path / "missing.json") is None
    wide = dataclasses.replace(ev, universe="sp500")
    save_evidence(path, wide)
    assert load_evidence(path).universe == "sp500"


def gate(signatures, verdict="ACCEPTED", live=None, as_of=date(2026, 9, 30)):
    return EvidenceGate(Evidence(signatures, as_of, "id", verdict), live or {})


TODAY = pd.Timestamp("2026-10-05")


def test_gate_passes_strong_signature():
    g = gate({"A": SignatureEvidence(150, 120, 0.3)}).gate("A", TODAY, RISK)
    assert g.passed
    assert g.win_rate == pytest.approx(0.8)


def test_gate_rejects_unknown_and_weak_signatures():
    eg = gate({"W": SignatureEvidence(150, 90, 0.3)})
    assert not eg.gate("A", TODAY, RISK).passed
    assert not eg.gate("W", TODAY, RISK).passed


def test_gate_requires_accepted_backtest():
    g = gate({"A": SignatureEvidence(150, 120, 0.3)}, verdict="REJECTED").gate("A", TODAY, RISK)
    assert not g.passed
    assert "backtest" in g.reason


def test_gate_rejects_stale_evidence():
    eg = gate({"A": SignatureEvidence(150, 120, 0.3)}, as_of=date(2026, 5, 1))
    g = eg.gate("A", TODAY, RISK)
    assert not g.passed
    assert "stale" in g.reason


def test_live_results_disable_signature():
    live = {"A": [True] * 17 + [False] * 13}  # last 30: 57% < 60%
    g = gate({"A": SignatureEvidence(150, 120, 0.3)}, live=live).gate("A", TODAY, RISK)
    assert not g.passed
    assert "disabled" in g.reason
    assert gate({"A": SignatureEvidence(150, 120, 0.3)}, live=live).disabled() == ["A"]


def test_save_evidence_with_numpy_values(tmp_path):
    """Real backtests produce numpy floats (prices come from numpy arrays)."""
    import numpy as np

    days = pd.bdate_range("2020-01-01", periods=5)
    trades = [
        Trade("X", "A", d, d, d, 100.0, 95.0, 1, np.float64(5.0), np.float64(1.0), "stop")
        for d in days
    ]
    ev = evidence_from_trades(trades, as_of=days[-1] + pd.Timedelta(days=1))
    assert type(ev["A"].wins) is int and type(ev["A"].mean_r) is float
    save_evidence(tmp_path / "e.json", Evidence(ev, date(2020, 1, 8), "id", "REJECTED"))
    assert load_evidence(tmp_path / "e.json").signatures["A"].n == 5
    assert type(trades[0].win) is bool


def test_gate_accepts_stability_advisory_verdict_only_explicitly():
    sig = {"A": SignatureEvidence(150, 120, 0.3)}
    assert gate(sig, verdict="ACCEPTED (stability advisory)").gate("A", TODAY, RISK).passed
    for v in ("PROVISIONAL (stability check not run)", "REJECTED (unstable)", "ACCEPTEDISH"):
        assert not gate(sig, verdict=v).gate("A", TODAY, RISK).passed


# ------------------------------------------------------- paper trial (decisions.md 2026-10-05)


def trial(signatures, verdict="REJECTED", live=None, as_of=date(2026, 9, 30)):
    return EvidenceGate(Evidence(signatures, as_of, "id", verdict), live or {}, paper_trial=True)


def test_paper_trial_waives_only_the_strategy_verdict():
    strong = {"A": SignatureEvidence(150, 120, 0.3)}
    for v in ("REJECTED", "REJECTED (unstable)", "PROVISIONAL (stability check not run)"):
        assert trial(strong, verdict=v).gate("A", TODAY, RISK).passed
    # Everything else still applies: weak or unknown signatures, stale evidence, live disabling.
    assert not trial({"A": SignatureEvidence(150, 80, 0.1)}).gate("A", TODAY, RISK).passed
    assert not trial(strong).gate("B", TODAY, RISK).passed
    stale = trial(strong, as_of=date(2026, 1, 1)).gate("A", TODAY, RISK)
    assert not stale.passed and "stale" in stale.reason
    disabled = trial(strong, live={"A": [False] * 30}).gate("A", TODAY, RISK)
    assert not disabled.passed and "disabled" in disabled.reason


def test_without_the_trial_a_rejected_backtest_still_blocks():
    g = EvidenceGate(
        Evidence({"A": SignatureEvidence(150, 120, 0.3)}, TODAY.date(), "id", "REJECTED"), {}
    )
    assert not g.gate("A", TODAY, RISK).passed


def test_evidence_file_prefers_the_strategys_own_file(tmp_path):
    base = tmp_path / "signatures.json"
    assert evidence_file(base, "confluence_reversion") == base
    own = tmp_path / "confluence_reversion.json"
    own.write_text("{}")
    assert evidence_file(base, "confluence_reversion") == own
    assert evidence_file(base, "trend_pullback") == base
    assert evidence_file(base, "confluence_reversion", "sp500") == base  # none yet
    wide = base.with_name("confluence_reversion-sp500.json")
    wide.write_text("{}")
    assert evidence_file(base, "confluence_reversion", "sp500") == wide
    assert evidence_file(base, "confluence_reversion") == own
