# Author: Kai0421
"""Tests for the pluggable strategy interface, the registry and the comparison report."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest import BacktestConfig, prepare, run_backtest, stability_check
from bot.compare import buy_and_hold, compare_strategies, render_comparison
from bot.config import ConfigError, RiskConfig, Settings, StrategyConfig
from bot.evidence import Evidence, SignatureEvidence, load_evidence, save_evidence
from bot.strategies import (
    ExitAction,
    PositionState,
    Strategy,
    as_strategy,
    get_strategy,
    registry,
    strategy_for,
)
from bot.strategies.rsi2_reversion import Rsi2Config, Rsi2Reversion
from bot.strategies.trend_pullback import TrendPullback
from bot.strategy import compute_features, entry_table, regime_table

RISK = RiskConfig()
BT = BacktestConfig(start=date(2018, 1, 1), end=date(2020, 12, 31), gated=False)


def ohlcv(closes, idx, spread=0.008, volume=2_000_000):
    c = np.asarray(closes, dtype="float64")
    opens = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, c) * (1 + spread),
            "low": np.minimum(opens, c) * (1 - spread),
            "close": c,
            "volume": volume,
        },
        index=idx,
    )


def market(n=600, n_symbols=4, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    bars = {"SPY": ohlcv(100 * np.cumprod(1 + rng.normal(0.0006, 0.008, n)), idx, 0.004)}
    sectors = {"SPY": "Broad"}
    for k in range(n_symbols):
        bars[f"S{k}"] = ohlcv(100 * np.cumprod(1 + rng.normal(0.0012, 0.018, n)), idx)
        sectors[f"S{k}"] = f"X{k}"
    return bars, sectors


def pos(**kw):
    base = dict(
        symbol="X",
        entry_price=100.0,
        initial_stop=95.0,
        stop=95.0,
        shares=100,
        days_held=1,
        partial_taken=False,
    )
    return PositionState(**(base | kw))


# ------------------------------------------------------- registry and settings


def test_registry_lists_all_strategies():
    assert set(registry()) == {
        "trend_pullback",
        "rsi2_reversion",
        "analyst_desk",
        "ibs_reversion",
        "confluence_reversion",
        "high_momentum",
        "kronos_rank",
    }
    assert isinstance(get_strategy("rsi2_reversion"), Rsi2Reversion)
    with pytest.raises(ValueError, match="unknown strategy"):
        get_strategy("martingale")


def test_as_strategy_wraps_a_trend_pullback_config():
    cfg = StrategyConfig(time_stop_days=12)
    s = as_strategy(cfg)
    assert isinstance(s, TrendPullback) and s.config is cfg
    assert as_strategy(s) is s
    with pytest.raises(TypeError):
        as_strategy("trend_pullback")


def test_settings_strategy_name_from_env():
    assert Settings.from_env({}).strategy_name == "trend_pullback"
    s = Settings.from_env({"STRATEGY": "RSI2_Reversion"})
    assert s.strategy_name == "rsi2_reversion"
    assert isinstance(strategy_for(s), Rsi2Reversion)
    with pytest.raises(ConfigError, match="STRATEGY"):
        Settings.from_env({"STRATEGY": "yolo"})


def test_strategy_for_passes_trend_pullback_parameters():
    cfg = StrategyConfig(exit_set="trend")
    assert strategy_for(Settings(strategy=cfg)).config is cfg


# ------------------------------------------------------- Trend Pullback adapter


def test_trend_pullback_matches_the_rules_module():
    bars, _ = market()
    cfg = StrategyConfig()
    features, tables = TrendPullback(cfg).prepare(bars, "SPY")
    regime = regime_table(compute_features(bars["SPY"], cfg), cfg)
    for s, df in bars.items():
        expected = entry_table(compute_features(df, cfg), regime, cfg)
        pd.testing.assert_series_equal(tables[s]["setup"], expected["setup"])
        pd.testing.assert_series_equal(tables[s]["signature"], expected["signature"])
        pd.testing.assert_series_equal(
            tables[s]["score"], features[s]["momentum"], check_names=False
        )


def test_trend_pullback_signatures_stay_unprefixed():
    bars, _ = market()
    _, tables = prepare(bars, StrategyConfig())
    sigs = [s for t in tables.values() for s in t["signature"].dropna()]
    assert sigs and not any(s.startswith("trend_pullback") for s in sigs)


# ------------------------------------------------------- base-class hard rules


class Reckless(Strategy):
    """Tries to break every rule the base class enforces."""

    name = "reckless"

    @classmethod
    def default_config(cls):
        return StrategyConfig()

    def features(self, bars):
        return bars.assign(atr=1.0)

    def entry_tables(self, features, benchmark):
        return {
            s: pd.DataFrame({"setup": True, "signature": "x", "score": 0.0}, index=f.index)
            for s, f in features.items()
        }

    def initial_stop(self, entry, atr):
        return entry + 1  # above the entry

    def on_close(self, pos, bar):
        return [ExitAction("dump", pos.shares * 3), ExitAction("again", 1)], pos.stop - 10


def test_stop_must_be_below_entry():
    with pytest.raises(ValueError, match="not between 0 and entry"):
        Reckless().stop_for(100.0, 2.0)


def test_manage_never_lowers_stop_or_oversells():
    actions, stop = Reckless().manage(pos(), {"close": 99.0})
    assert stop == 95.0
    assert actions == [ExitAction("dump", 100)]


def test_prepare_prefixes_signatures_and_validates_columns():
    bars, _ = market(n=60, n_symbols=1)
    _, tables = Reckless().prepare(bars, "SPY")
    assert set(tables["S0"]["signature"]) == {"reckless|x"}

    class NoAtr(Reckless):
        def features(self, bars):
            return bars

    with pytest.raises(ValueError, match="missing"):
        NoAtr().prepare(bars, "SPY")
    with pytest.raises(ValueError, match="benchmark"):
        Reckless().prepare({"S0": bars["S0"]}, "SPY")


# ------------------------------------------------------- RSI(2) reversion


def test_rsi2_config_rejects_missing_stop():
    with pytest.raises(ConfigError):
        Rsi2Config(stop_atr_mult=0)
    with pytest.raises(ConfigError):
        Rsi2Config(sma_exit=300)


def test_rsi2_setups_have_no_look_ahead():
    bars, _ = market()
    s = Rsi2Reversion()
    _, full = s.prepare(bars, "SPY")
    cut = bars["SPY"].index[450]
    _, prefix = s.prepare({k: v.loc[:cut] for k, v in bars.items()}, "SPY")
    for sym in bars:
        pd.testing.assert_frame_equal(prefix[sym], full[sym].loc[:cut])
    sigs = [x for t in full.values() for x in t["signature"].dropna()]
    assert sigs and all(x.startswith("rsi2_reversion|") for x in sigs)


def test_rsi2_exits():
    s = Rsi2Reversion()
    assert s.manage(pos(), {"close": 101.0, "sma_exit": 100.0}) == (
        [ExitAction("sma_exit", 100)],
        95.0,
    )
    assert s.manage(pos(days_held=10), {"close": 98.0, "sma_exit": 99.0})[0] == [
        ExitAction("time_stop", 100)
    ]
    assert s.manage(pos(), {"close": 98.0, "sma_exit": 99.0}) == ([], 95.0)
    assert s.stop_for(100.0, 2.0) == pytest.approx(95.0)


def test_rsi2_backtest_runs_end_to_end():
    bars, sectors = market()
    res = run_backtest(bars, sectors, Rsi2Reversion(), RISK, BT)
    assert res.strategy == "rsi2_reversion"
    assert res.signal_trades
    assert {t.exit_reason for t in res.signal_trades} <= {
        "stop",
        "sma_exit",
        "time_stop",
        "end_of_data",
    }
    assert all(t.signature.startswith("rsi2_reversion|") for t in res.portfolio.trades)


def test_stability_uses_the_strategy_parameters():
    bars, sectors = market(n=400, n_symbols=2)
    results = stability_check(bars, sectors, Rsi2Reversion(), RISK, BT, params=("sma_exit",))
    assert [r.label for r in results] == ["sma_exit x0.8", "sma_exit x1.2"]


# ------------------------------------------------------- evidence


def test_evidence_records_strategy_and_reads_older_files(tmp_path):
    path = tmp_path / "e.json"
    sig = {"A": SignatureEvidence(150, 120, 0.3)}
    save_evidence(path, Evidence(sig, date(2026, 9, 30), "id", "ACCEPTED", "rsi2_reversion"))
    assert load_evidence(path).strategy == "rsi2_reversion"
    raw = path.read_text().replace('"strategy": "rsi2_reversion",', "")
    path.write_text(raw)
    assert load_evidence(path).strategy == "trend_pullback"


# ------------------------------------------------------- comparison


def test_compare_runs_each_strategy_on_the_same_data():
    bars, sectors = market()
    strategies = [TrendPullback(StrategyConfig(exit_set="trend")), Rsi2Reversion()]
    results = compare_strategies(bars, sectors, strategies, RISK, BT)
    assert [s.name for s, _ in results] == ["trend_pullback", "rsi2_reversion"]
    assert results[1][1].metrics.trades > 0
    text = render_comparison(results, RISK, BT, "cmp-test")
    assert "# Strategy comparison cmp-test" in text
    assert "| `trend_pullback` |" in text and "| `rsi2_reversion` |" in text
    assert "survivorship" in text.lower()
    assert "## Suggestion" not in text
    assert "Drawdown pause: permanent" in text and "| Pauses |" in text
    resumed = render_comparison(
        results, RISK, dataclasses.replace(BT, resume_after_days=20), "cmp-test"
    )
    assert "restarts 20 trading days after a pause" in resumed
    suggested = render_comparison(results, RISK, BT, "cmp-test", current="trend_pullback")
    assert "## Suggestion" in suggested
    with pytest.raises(ValueError, match="once"):
        compare_strategies(bars, sectors, [Rsi2Reversion(), Rsi2Reversion()], RISK, BT)


def test_report_shows_sizing_and_the_spy_benchmark():
    bars, sectors = market()
    results = compare_strategies(bars, sectors, [Rsi2Reversion()], RISK, BT)
    small = RiskConfig(risk_per_trade=0.0025, max_position_pct=0.05, max_open_positions=3)
    text = render_comparison(results, small, BT, "cmp", benchmark=buy_and_hold(bars["SPY"], BT))
    assert "risk 0.25% per trade, at most 3 positions, 5% of equity each" in text
    assert "SPY buy and hold" in text


def test_buy_and_hold_return_and_drawdown():
    idx = pd.bdate_range("2020-01-01", periods=4)
    spy = pd.DataFrame({"close": [100.0, 120.0, 90.0, 110.0]}, index=idx)
    bt = BacktestConfig(start=date(2020, 1, 1), end=date(2020, 1, 6), gated=False)
    b = buy_and_hold(spy, bt)
    assert b.total_return == pytest.approx(0.10)
    assert b.max_drawdown == pytest.approx(0.25)
    assert "+10.0% total" in b.text and "25.0%" in b.text


def test_compare_main_refuses_bad_input(capsys):
    from bot.compare import main

    args = ["--start", "2016-01-01", "--end", "2026-09-30"]
    assert main([*args, "--strategies", "nope"], env={}) == 2
    assert "unknown strategy" in capsys.readouterr().err
    assert main(args, env={}) == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_with_config_returns_a_new_instance():
    s = Rsi2Reversion()
    t = s.with_config(dataclasses.replace(s.config, rsi_entry_below=5.0))
    assert isinstance(t, Rsi2Reversion) and t.config.rsi_entry_below == 5.0
    assert s.config.rsi_entry_below == 10.0
