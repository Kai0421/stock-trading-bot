# Author: Kai0421
"""Tests for the strategy suggestion: STRATEGY_CANDIDATES and bot.compare.suggest."""

from types import SimpleNamespace as NS

import pytest

from bot.compare import suggest
from bot.config import ConfigError, Settings
from bot.strategies import get_strategy


def result(expectancy, lb, failures=()):
    return NS(
        metrics=NS(expectancy_r=expectancy, wilson_lb=lb, trades=150), failures=list(failures)
    )


def pairs(*rows):
    return [(get_strategy(name), result(*args)) for name, *args in rows]


def test_candidates_from_env():
    assert Settings.from_env({}).strategy_candidates == ()
    s = Settings.from_env({"STRATEGY_CANDIDATES": " trend_pullback, Analyst_Desk "})
    assert s.strategy_candidates == ("trend_pullback", "analyst_desk")
    with pytest.raises(ConfigError, match="STRATEGY_CANDIDATES"):
        Settings.from_env({"STRATEGY_CANDIDATES": "trend_pullback,nope"})


def test_suggests_best_passing_strategy():
    s = suggest(
        pairs(
            ("trend_pullback", 0.04, 0.63),
            ("analyst_desk", 0.09, 0.64),
            ("rsi2_reversion", 0.20, 0.55, ["win-rate lower bound below floor"]),
        ),
        current="trend_pullback",
    )
    assert s.name == "analyst_desk" and s.switch
    assert "STRATEGY=analyst_desk" in s.text and "decisions.md" in s.text


def test_keeps_current_when_it_is_best():
    s = suggest(pairs(("trend_pullback", 0.05, 0.63), ("analyst_desk", 0.02, 0.62)),
                current="trend_pullback")  # fmt: skip
    assert s.name == "trend_pullback" and not s.switch
    assert "keep" in s.text.lower()


def test_no_suggestion_when_nothing_passes():
    s = suggest(pairs(("analyst_desk", 0.3, 0.5, ["too few trades"])), current="trend_pullback")
    assert s.name is None and not s.switch
    assert "trend_pullback" in s.text


def test_tie_breaks_on_lower_bound():
    s = suggest(pairs(("rsi2_reversion", 0.05, 0.62), ("analyst_desk", 0.05, 0.66)),
                current="trend_pullback")  # fmt: skip
    assert s.name == "analyst_desk"


def test_equity_csv_has_month_end_equity_per_strategy():
    import pandas as pd

    from bot.compare import equity_csv

    days = pd.bdate_range("2024-01-01", "2024-03-29")
    eq = pd.Series(range(len(days)), index=days, dtype=float) + 100_000
    rows = [(get_strategy("trend_pullback"), NS(portfolio=NS(equity=eq)))]
    lines = equity_csv(rows, "expectancy").splitlines()
    assert lines[0] == "gate_mode,strategy,month,equity"
    assert lines[1:] == [
        "expectancy,trend_pullback,2024-01,100022",
        "expectancy,trend_pullback,2024-02,100043",
        "expectancy,trend_pullback,2024-03,100064",
    ]
