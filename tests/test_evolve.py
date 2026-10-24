# Author: Kai0421
"""Tests for the generational walk-forward tuner (bot.evolve)."""

import dataclasses
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest import Trade
from bot.config import RiskConfig
from bot.evolve import (
    EvolveConfig,
    Score,
    evolve,
    fitness,
    holdout_trades,
    mutations,
    promote,
    render_evolution,
    training_trades,
)
from bot.strategies import get_strategy
from bot.strategies.rsi2_reversion import Rsi2Reversion
from test_research_strategies import market


def trade(exit_day: str, r: float, entry_day: str | None = None) -> Trade:
    exit_ts = pd.Timestamp(exit_day)
    entry_ts = pd.Timestamp(entry_day) if entry_day else exit_ts - pd.Timedelta(days=3)
    return Trade(
        symbol="X",
        signature="s",
        signal_date=entry_ts - pd.Timedelta(days=1),
        entry_date=entry_ts,
        exit_date=exit_ts,
        entry_price=100.0,
        initial_stop=98.0,
        shares=100,
        pnl=r * 200.0,
        r_multiple=r,
        exit_reason="stop" if r < 0 else "sma_exit",
    )


# ------------------------------------------------------- scoring


def test_fitness_follows_the_gate_mode():
    trades = [trade("2020-01-10", 0.5)] * 70 + [trade("2020-01-10", -1.0)] * 30
    win_rate = fitness(trades, RiskConfig())
    assert win_rate.n == 100 and win_rate.win_rate == pytest.approx(0.70)
    assert win_rate.value == pytest.approx(win_rate.wilson_lb)
    exp = fitness(trades, RiskConfig(gate_mode="expectancy"))
    assert exp.value == pytest.approx(exp.mean_r_lb)
    assert exp.mean_r == pytest.approx(0.05)


def test_fitness_ranks_negative_expectancy_below_any_positive_one():
    losing = [trade("2020-01-10", 0.1)] * 80 + [trade("2020-01-10", -1.0)] * 20  # 80% wins
    winning = [trade("2020-01-10", 1.0)] * 50 + [trade("2020-01-10", -0.5)] * 50
    assert fitness(losing, RiskConfig()).mean_r < 0
    assert fitness(winning, RiskConfig()).value > fitness(losing, RiskConfig()).value


def test_fitness_of_no_trades_is_minus_infinity():
    s = fitness([], RiskConfig())
    assert s.n == 0 and s.value == -math.inf


def test_promotion_needs_enough_trades_and_a_margin():
    cfg = EvolveConfig(min_test_trades=30, margin=0.01)
    champ = Score(50, 0.6, 0.5, 0.02, -0.01, 0.50)
    better = Score(40, 0.7, 0.6, 0.05, 0.01, 0.60)
    assert promote(champ, better, cfg)
    assert not promote(champ, dataclasses.replace(better, n=10), cfg)
    assert not promote(champ, dataclasses.replace(better, value=0.505), cfg)
    assert promote(Score(0, math.nan, math.nan, math.nan, math.nan, -math.inf), better, cfg)


# ------------------------------------------------------- data splits


def test_train_and_test_windows_never_overlap():
    trades = [
        trade("2019-12-10", 1.0),  # exits well before the embargo
        trade("2019-12-28", 1.0),  # exits inside the embargo: dropped from training
        trade("2020-01-08", 1.0, entry_day="2019-12-30"),  # entered before the test year
        trade("2020-03-01", 1.0, entry_day="2020-02-25"),
        trade("2021-01-05", 1.0, entry_day="2020-12-30"),  # entered in the test year
    ]
    train = training_trades(trades, 2020, embargo_days=10)
    assert [t.exit_date for t in train] == [pd.Timestamp("2019-12-10")]
    test = holdout_trades(trades, 2020)
    assert [t.entry_date for t in test] == [pd.Timestamp("2020-02-25"), pd.Timestamp("2020-12-30")]


# ------------------------------------------------------- mutations


def test_mutations_change_one_parameter_by_the_step_and_stay_valid():
    base = Rsi2Reversion()
    cfg = EvolveConfig(challengers=0, step=0.2)
    children = mutations(base, cfg, np.random.default_rng(1))
    assert children
    for child in children:
        changed = {
            f.name
            for f in dataclasses.fields(base.config)
            if getattr(child.config, f.name) != getattr(base.config, f.name)
        }
        assert len(changed) == 1
        (name,) = changed
        assert name in base.stability_params
        old, new = getattr(base.config, name), getattr(child.config, name)
        assert abs(new / old - 1) <= 0.2 + 1e-9 or abs(new - old) == 1
    assert len({c.config for c in children}) == len(children)


def test_random_challengers_are_reproducible():
    base = get_strategy("ibs_reversion")
    cfg = EvolveConfig(challengers=5)
    a = [c.config for c in mutations(base, cfg, np.random.default_rng(3))]
    b = [c.config for c in mutations(base, cfg, np.random.default_rng(3))]
    assert a == b and len(a) > len(base.stability_params) * 2 - 1


# ------------------------------------------------------- the generational loop


@pytest.fixture(scope="module")
def run():
    bars, _ = market(n=1100, n_symbols=5)
    cfg = EvolveConfig(
        first_test_year=2019,
        last_test_year=2020,
        challengers=2,
        min_train_trades=20,
        min_test_trades=5,
    )
    return evolve(bars, get_strategy("ibs_reversion"), RiskConfig(), cfg), cfg


def test_one_generation_per_test_year(run):
    result, _ = run
    assert [g.year for g in result.generations] == [2019, 2020]
    assert result.trials >= len(result.generations)
    first = result.generations[0]
    assert first.champion == get_strategy("ibs_reversion").config  # generation 0 = defaults


def test_lineage_trades_only_with_parameters_chosen_before_each_year(run):
    result, _ = run
    for g in result.generations:
        lineage_year = [t for t in result.lineage_trades if t.entry_date.year == g.year]
        assert len(lineage_year) == g.champion_test.n
    # The next champion is the promoted challenger or the same champion.
    for prev, nxt in zip(result.generations, result.generations[1:], strict=False):
        assert nxt.champion == (prev.challenger if prev.promoted else prev.champion)


def test_report_compares_lineage_with_the_static_defaults(run):
    result, cfg = run
    text = render_evolution([result], RiskConfig(), cfg, "evolve-test", date(2017, 1, 2))
    assert "# Strategy evolution evolve-test" in text
    assert "| 2019 |" in text and "| 2020 |" in text
    assert "Static defaults" in text and "Champion lineage" in text
    assert "never changes what the bot trades" in text


def test_evolve_main_refuses_bad_input(capsys):
    from bot.evolve import main

    args = ["--start", "2016-01-01", "--end", "2026-09-30"]
    assert main([*args, "--strategies", "nope"], env={}) == 2
    assert "unknown strategy" in capsys.readouterr().err
    assert main([*args, "--first-test-year", "2015"], env={}) == 2
    assert main(args, env={}) == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err
