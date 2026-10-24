# Author: Kai0421
"""The learning loop: post-mortems, the pattern miner, challenger rules and the safety rules.

Everything here is pure (DataFrames in, DataFrames and text out). Nothing can place an order and
nothing in the order path imports ``bot.learning``.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot import learning as lg

SRC = Path(__file__).resolve().parents[1] / "src" / "bot"


# ------------------------------------------------------------------ order path untouched


@pytest.mark.parametrize("module", ["engine", "decide", "execute", "gather", "config", "risk"])
def test_order_path_does_not_import_the_learning_loop(module):
    text = (SRC / f"{module}.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(import|from)\s+bot(\.|\s+import\s+)(learning)", text)
    assert "learning" not in re.findall(r"from bot import ([\w, ]+)", text)


# ------------------------------------------------------------------ small maths


def test_binomial_tail_known_values():
    assert lg.binom_sf(10, 10, 0.5) == pytest.approx(1 / 1024)
    assert lg.binom_sf(0, 10, 0.5) == pytest.approx(1.0)
    assert lg.binom_sf(5, 10, 0.5) == pytest.approx(0.623046875)
    assert lg.binom_sf(1, 10, 0.0) == 0.0
    assert lg.binom_sf(3, 10, 1.0) == 1.0


def test_binomial_tail_is_stable_for_big_samples():
    p = lg.binom_sf(2500, 5000, 0.5)
    assert 0.4 < p < 0.55


def test_benjamini_hochberg_adjusts_in_order():
    q = lg.bh_adjust([0.01, 0.04, 0.03, 0.2])
    assert q == pytest.approx([0.04, 0.053333, 0.053333, 0.2], abs=1e-4)
    assert lg.bh_adjust([]) == []


# ------------------------------------------------------------------ exit kinds and risk


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("stop", "stop"),
        ("trail", "stop"),
        ("trend_break", "trend_break"),
        ("profit_target", "profit_target"),
        ("late", "late"),
        ("time", "time_limit"),
        ("council turned", "council_turned"),
        ("end_of_data", "other"),
        ("", "other"),
    ],
)
def test_exit_kind(reason, kind):
    assert lg.exit_kind(reason) == kind


def test_risk_per_share_prefers_the_stop_then_falls_back_to_r():
    assert lg.risk_per_share({"entry_price": 100, "initial_stop": 95}) == 5
    t = {"entry_price": 100, "exit_price": 105, "r": 1.0}
    assert lg.risk_per_share(t) == pytest.approx(5.0)
    assert math.isnan(lg.risk_per_share({"entry_price": 100, "exit_price": 100, "r": 0.0}))


# ------------------------------------------------------------------ post-mortems


def bars(rows):
    idx = pd.bdate_range("2026-01-02", periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


def stopped_trade():
    return {
        "id": 1, "source": "live", "symbol": "AAA", "sector": "Health Care", "signature": "s1",
        "entry_date": "2026-01-06", "exit_date": "2026-01-08", "entry_price": 100.0,
        "exit_price": 95.0, "shares": 10, "pnl": -50.0, "r": -1.0, "exit_reason": "stop",
        "initial_stop": 95.0, "votes_for": "",
    }  # fmt: skip


def test_post_mortem_of_a_stop_that_then_recovered():
    # entry 6 Jan (open 100); high 103 / low 99 on 6th; stop 95 hit on 8th; price recovers after
    b = bars([
        (99, 100, 98, 99, 1e6),       # 2 Jan (day before the entry is 5th)
        (99, 100, 98, 99, 1e6),       # 5 Jan
        (100, 103, 99, 101, 1e6),     # 6 Jan entry
        (101, 102, 99.5, 100, 1e6),   # 7 Jan
        (100, 100.5, 94, 95, 1e6),    # 8 Jan stop out
        (96, 101, 95, 100.5, 1e6),    # 9 Jan recovers above the entry
        (100, 102, 99, 101, 1e6),
    ])  # fmt: skip
    pm = lg.post_mortem(stopped_trade(), b, None)
    assert pm["exit_kind"] == "stop"
    assert pm["win"] is False
    assert pm["mfe_r"] == pytest.approx(0.6)  # best high 103 -> +3 on risk 5
    assert pm["mae_r"] == pytest.approx(1.2)  # low 94 -> -6 on risk 5
    assert pm["recovered_after_exit"] is True
    assert "recover" in pm["lesson"].lower()
    assert "$50" in pm["lesson"]


def test_post_mortem_of_a_stop_that_kept_falling():
    b = bars([
        (99, 100, 98, 99, 1e6),
        (99, 100, 98, 99, 1e6),
        (100, 103, 99, 101, 1e6),
        (101, 102, 99.5, 100, 1e6),
        (100, 100.5, 94, 95, 1e6),
        (94, 95, 90, 91, 1e6),
        (91, 92, 88, 89, 1e6),
    ])  # fmt: skip
    pm = lg.post_mortem(stopped_trade(), b, None)
    assert pm["recovered_after_exit"] is False
    assert "did its job" in pm["lesson"]


def test_post_mortem_without_bars_still_gives_a_line():
    pm = lg.post_mortem(stopped_trade(), None, None)
    assert math.isnan(pm["mae_r"]) and pm["recovered_after_exit"] is None
    assert pm["lesson"].startswith("AAA")


def test_post_mortem_regime_and_context_columns():
    idx = pd.bdate_range("2025-01-01", periods=260)
    spy = pd.DataFrame({"close": np.linspace(100, 200, 260)}, index=idx)  # rising: above SMA200
    b = bars([(99, 100, 98, 99, 1e6)] * 3 + [(100, 103, 99, 101, 1e6)] * 4)
    t = stopped_trade() | {"entry_date": "2026-01-06"}
    spy2 = spy.copy()
    spy2.index = pd.bdate_range(end="2026-01-09", periods=260)
    pm = lg.post_mortem(
        t, b, spy2, {"atr": 2.0, "close": 100.0, "flags": ["earnings date unknown"]}
    )
    assert pm["regime"] == "market_up"
    assert pm["atr_pct"] == pytest.approx(0.02)
    assert "earnings" in pm["risk_flags"]


def test_lessons_for_each_exit_kind_are_plain_english():
    base = stopped_trade()
    for reason, pnl, r in [
        ("trend_break", -80.0, -0.4), ("profit_target", 120.0, 0.6), ("late", -265.2, -0.33),
        ("time", 30.0, 0.1), ("council turned", -20.0, -0.1),
    ]:  # fmt: skip
        pm = lg.post_mortem(base | {"exit_reason": reason, "pnl": pnl, "r": r}, None, None)
        line = pm["lesson"]
        assert "AAA" in line and "$" in line
        assert "_" not in line  # no internal codes
        assert "R multiple" not in line


# ------------------------------------------------------------------ normalising the sources


def test_normalise_live_joins_signal_features_and_sector():
    trades = pd.DataFrame([{
        "id": 7, "symbol": "MDT", "entry_date": "2026-10-14", "exit_date": "2026-10-15",
        "entry_price": 92.81, "exit_price": 97.64, "shares": 207, "pnl": 449.19,
        "r_multiple": 0.4657, "exit_reason": "profit_target", "holding_days": 1,
        "signature": "sig", "win_prob": 0.7,
    }])  # fmt: skip
    signals = pd.DataFrame([{
        "day": "2026-10-14", "symbol": "MDT", "signature": "sig", "taken": 1,
        "features": '{"close": 93.01, "atr": 2.4, "flags": ["earnings date unknown"]}',
    }])  # fmt: skip
    out = lg.normalise_live(trades, signals, {"MDT": "Health Care"})
    row = out.iloc[0]
    assert row["source"] == "live" and row["sector"] == "Health Care"
    assert row["r"] == pytest.approx(0.4657)
    assert row["atr"] == pytest.approx(2.4) and row["close"] == pytest.approx(93.01)
    assert "earnings" in row["flags"]


def test_normalise_council_keeps_closed_trades_only():
    ledger = pd.DataFrame([
        {"id": "a", "symbol": "TMO", "sector": "Health Care", "votes_for": "Crowd Rick",
         "entry_date": "2026-10-14", "entry_price": 664.0, "shares": 28, "initial_stop": 628.0,
         "status": "closed", "exit_date": "2026-10-16", "exit_price": 650.0,
         "exit_reason": "stop", "pnl": -400.0, "r": -0.4},
        {"id": "b", "symbol": "V", "sector": "Financials", "votes_for": "Rick C127",
         "entry_date": "2026-10-14", "entry_price": 395.0, "shares": 50, "initial_stop": 380.0,
         "status": "open", "exit_date": None, "exit_price": None, "exit_reason": "",
         "pnl": None, "r": None},
    ])  # fmt: skip
    out = lg.normalise_council(ledger)
    assert list(out["symbol"]) == ["TMO"] and out.iloc[0]["source"] == "council"


# ------------------------------------------------------------------ the pattern miner


def synthetic(n=400, planted=None, seed=1, n_sectors=6):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2023-01-02", periods=n)
    sectors = [f"S{i}" for i in range(n_sectors)]
    df = pd.DataFrame({
        "entry_date": days,
        "sector": rng.choice(sectors, n),
        "regime": rng.choice(["market_up", "market_down"], n),
        "weekday": rng.choice(["Mon", "Tue", "Wed", "Thu", "Fri"], n),
        "atr_bucket": rng.choice(["low", "mid", "high"], n),
    })  # fmt: skip
    p_loss = np.full(n, 0.42)
    if planted:
        dim, value, rate = planted
        p_loss[(df[dim] == value).to_numpy()] = rate
    lose = rng.random(n) < p_loss
    df["win"] = ~lose
    df["r"] = np.where(lose, -1.0, 0.8) + rng.normal(0, 0.2, n)
    df["pnl"] = df["r"] * 100
    return df


DIMS = ["sector", "regime", "weekday", "atr_bucket"]


def test_miner_finds_a_planted_losing_cluster():
    found = lg.mine(synthetic(planted=("sector", "S2", 0.80), n=600), DIMS)
    assert found.status == "ok"
    hits = [(x.dim, x.value) for x in found.lessons]
    assert ("sector", "S2") in hits
    top = next(x for x in found.lessons if x.value == "S2")
    assert top.strength in ("moderate", "strong")
    assert top.n_test >= 5 and top.loss_rate > top.rest_loss_rate
    assert top.win_low <= top.win_high


def test_miner_rejects_noise():
    false_found = 0
    for seed in range(40):
        res = lg.mine(synthetic(seed=100 + seed, n=500), DIMS)
        false_found += len(res.lessons)
    assert false_found <= 2  # 40 noise datasets: at most a rare slip, never a flood


def test_miner_says_too_early_with_few_trades():
    res = lg.mine(synthetic(n=12), DIMS)
    assert res.status == "too_early"
    assert res.lessons == []
    assert "too early" in res.message.lower()


def test_unconfirmed_cluster_is_only_a_watch_item():
    # a losing cluster that exists in the first 60% of time only must (almost) never become a
    # lesson: it is fitted on the early trades but the later trades do not back it up
    as_lesson = as_watch = 0
    for seed in range(12):
        df = synthetic(n=600, seed=50 + seed)
        cut = int(len(df) * 0.6)
        rng = np.random.default_rng(900 + seed)
        first = df.index < cut
        s3 = (df["sector"] == "S3").to_numpy()
        lose = rng.random(len(df)) < np.where(first & s3, 0.9, 0.42)
        df["win"] = ~lose
        df["r"] = np.where(lose, -1.0, 0.8)
        res = lg.mine(df, DIMS)
        as_lesson += ("sector", "S3") in [(x.dim, x.value) for x in res.lessons]
        as_watch += ("sector", "S3") in [(x.dim, x.value) for x in res.watch]
    assert as_lesson <= 2
    assert as_watch >= 8


def test_lesson_text_is_plain_english():
    res = lg.mine(synthetic(planted=("sector", "S2", 0.85), n=600), DIMS)
    text = lg.lesson_text(res.lessons[0])
    assert "S2" in text and "%" in text and "trades" in text
    assert "p=" not in text and "q=" not in text and "FDR" not in text


# ------------------------------------------------------------------ challengers


def test_skip_context_challenger_keeps_the_rest():
    df = synthetic(planted=("sector", "S2", 0.85), n=800)
    ch = lg.skip_context("sector", "S2", "c-skip-s2")
    res = lg.compare(df, ch)
    assert res.skipped.n > 0 and res.kept.n + res.skipped.n == res.champion.n
    assert res.kept.mean_r > res.champion.mean_r
    assert res.proposable is True
    assert res.direction == "tighten"


def test_challenger_that_skips_good_trades_is_not_proposed():
    df = synthetic(n=800, seed=3)
    res = lg.compare(df, lg.skip_context("sector", "S1", "c-skip-s1"))
    assert res.proposable is False


def test_challenger_needs_enough_out_of_sample_skips():
    df = synthetic(planted=("sector", "S2", 0.9), n=80, seed=2, n_sectors=12)
    res = lg.compare(df, lg.skip_context("sector", "S2", "c-small"))
    assert res.proposable is False
    assert "not enough" in res.reason.lower()


def test_challenger_that_starves_the_sample_is_not_proposed():
    df = synthetic(n=800, seed=4)
    everything = lg.Challenger("c-all", "skip nearly everything", "skip_context", lambda r: False)
    res = lg.compare(df, everything)
    assert res.proposable is False


def test_compare_uses_only_the_later_trades_for_the_decision():
    df = synthetic(planted=("sector", "S2", 0.85), n=800)
    res = lg.compare(df, lg.skip_context("sector", "S2", "c"), test_frac=0.4)
    assert res.champion.n == int(round(len(df) * 0.4)) or abs(res.champion.n - len(df) * 0.4) <= 1


# ------------------------------------------------------------------ safety asymmetry


def test_tightening_kinds_may_be_shadow_pauses_loosening_needs_the_owner():
    assert lg.direction_of("skip_context") == "tighten"
    assert lg.direction_of("require_votes") == "tighten"
    assert lg.direction_of("wider_stop") == "loosen"
    assert lg.direction_of("bigger_size") == "loosen"
    assert lg.direction_of("something_new") == "loosen"  # unknown is never automatic


def test_forbidden_changes_are_refused():
    for kind in ("lower_gate", "remove_stop", "raise_risk", "leverage"):
        t = lg.triage_kind(kind)
        assert t.action == "refused"
        assert "owner" in t.message.lower()


def test_triage_of_results():
    df = synthetic(planted=("sector", "S2", 0.85), n=800)
    good = lg.triage(lg.compare(df, lg.skip_context("sector", "S2", "c-ok")))
    assert good.action == "shadow_pause"
    assert good.applied_to_live is False
    loose = lg.Challenger("c-wide", "wider stops", "wider_stop", lambda r: True)
    t = lg.triage(lg.compare(df, loose))
    assert t.action in ("owner_proposal", "none")
    assert t.applied_to_live is False


def test_owner_proposal_has_a_decision_entry_template():
    df = synthetic(n=800, seed=8)
    # a loosening change that "looks good" must still only be a proposal
    ch = lg.Challenger("c-wide", "wider stops", "wider_stop", lambda r: r["sector"] != "S9")
    res = lg.compare(df, ch)
    res = res.__class__(**{**res.__dict__, "proposable": True})
    t = lg.triage(res)
    assert t.action == "owner_proposal"
    assert "memory/decisions.md" in t.message and "reviewed commit" in t.message


# ------------------------------------------------------------------ report and digest


def test_report_on_real_sized_data_is_honest_about_too_early():
    pm = lg.post_mortem(stopped_trade(), None, None)
    rep = lg.build_report(pd.DataFrame([pm]), live_trades=1, council_trades=0)
    assert rep.status == "too_early"
    title, text = lg.digest(rep)
    assert "too early" in text.lower()
    assert "proposal" in text.lower() or "nothing to change" in text.lower()
    assert "_" not in text.replace("`", "")


def test_digest_lists_lessons_and_labels_advice_as_proposal():
    df = synthetic(planted=("sector", "S2", 0.85), n=800)
    df["id"] = range(len(df))
    df["lesson"] = "x"
    rep = lg.build_report(df, live_trades=0, council_trades=len(df), challengers=[
        lg.skip_context("sector", "S2", "c-skip-s2")
    ])  # fmt: skip
    title, text = lg.digest(rep)
    assert "S2" in text
    assert "proposal" in text.lower()
    assert "no real order" in text.lower() or "nothing was changed" in text.lower()
    assert not re.search(r"\b(q|p)\s*=", text)


def test_changelog_line_follows_the_shared_convention():
    line = lg.changelog_entry("c-skip-s2", "skip sector S2", "2026-10-17", "owner approval pending")
    assert line.startswith("### ") and "c-skip-s2" in line and "Approved by" in line
