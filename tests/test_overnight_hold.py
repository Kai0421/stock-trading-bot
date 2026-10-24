# Author: Kai0421
"""Tests for the overnight hold strategy (docs/19-overnight-hold.md).

Only ``TRADING_MODE=overnight`` (paper only, owner decision 2026-10-08) can trade it: it is
in neither strategy registry and STRATEGY alone cannot select it.
"""

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot.config import ConfigError, Settings
from bot.intraday import intraday_registry
from bot.overnight_hold import (
    NAME,
    SHADOW_COLUMNS,
    OvernightConfig,
    monthly_selection,
    overnight_returns,
    pick_symbols,
    record_shadow,
    run_overnight_backtest,
    settle_shadow,
    shadow_summary,
)
from bot.strategies import DEFAULT_STRATEGY, registry

SRC = Path(__file__).resolve().parents[1] / "src" / "bot"


def bars(opens, closes, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    o = np.asarray(opens, dtype="float64")
    c = np.asarray(closes, dtype="float64")
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c) * 1.01,
            "low": np.minimum(o, c) * 0.99,
            "close": c,
            "volume": 1_000_000,
        },
        index=idx,
    )


# ------------------------------------------------------------ selection rules
# Owner decision 2026-10-08: tradable on paper only through TRADING_MODE=overnight.


def test_not_a_swing_or_intraday_strategy():
    assert NAME not in registry()
    assert NAME not in intraday_registry()
    assert DEFAULT_STRATEGY != NAME


def test_swing_strategy_setting_rejects_it():
    with pytest.raises(ConfigError):
        Settings.from_env({"STRATEGY": NAME})


def test_overnight_mode_selects_it_and_only_it():
    assert Settings.from_env({"TRADING_MODE": "overnight"}).strategy_name == NAME
    with pytest.raises(ConfigError, match="overnight"):
        Settings.from_env({"TRADING_MODE": "overnight", "STRATEGY": "trend_pullback"})
    with pytest.raises(ConfigError, match="paper"):
        Settings.from_env({"TRADING_MODE": "overnight", "PAPER": "false"})


def test_default_settings_do_not_select_it():
    s = Settings.from_env({})
    assert s.strategy_name != NAME
    assert s.trading_mode == "swing"
    assert NAME not in s.strategy_candidates


@pytest.mark.parametrize(
    "path",
    sorted(
        p
        for p in SRC.rglob("*.py")
        if p.name not in {"overnight_hold.py", "overnight_live.py", "decide.py", "engine.py"}
    ),
)
def test_only_the_overnight_mode_imports_it(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        assert not any(n.endswith(("overnight_hold", "overnight_live")) for n in names), path


# ------------------------------------------------------------ config


def test_defaults_match_the_bot_rules_preset():
    cfg = OvernightConfig()
    assert cfg.alloc_pct == 0.05
    assert cfg.max_positions == 3
    assert cfg.cost_bps == 10
    assert cfg.night_filter == "all"
    assert "MU" in cfg.symbols


@pytest.mark.parametrize(
    "kwargs",
    [
        {"alloc_pct": 0},
        {"alloc_pct": 0.21},  # above the bot's per-position ceiling
        {"max_positions": 0},
        {"cost_bps": -1},
        {"night_filter": "fridays"},
        {"cash_reserve_pct": 0.01},
        {"symbols": ()},
        {"symbols": ("mu",)},
        {"select_top": -1},
        {"select_top": 2, "select_lookback": 1},
    ],
)
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(ConfigError):
        OvernightConfig(**kwargs)


# ------------------------------------------------------------ mechanics


def test_overnight_return_is_next_open_over_close():
    df = bars([10, 11, 12], [10, 10, 12])
    r = overnight_returns(df)
    assert r.iloc[0] == pytest.approx(11 / 10 - 1)
    assert r.iloc[1] == pytest.approx(12 / 10 - 1)
    assert np.isnan(r.iloc[2])  # no next open yet


def test_filters_choose_nights():
    # Day 1 falls, day 2 rises.
    df = {"AAA": bars([10, 10, 10, 10], [10, 9, 10, 10])}
    days = df["AAA"].index
    down = OvernightConfig(symbols=("AAA",), night_filter="down")
    up = OvernightConfig(symbols=("AAA",), night_filter="up")
    assert pick_symbols(df, days[1], down) == ["AAA"]
    assert pick_symbols(df, days[1], up) == []
    assert pick_symbols(df, days[2], up) == ["AAA"]


def test_notfri_skips_fridays():
    df = {"AAA": bars([10] * 6, [10] * 6, start="2024-01-01")}  # Mon..Mon
    cfg = OvernightConfig(symbols=("AAA",), night_filter="notfri")
    friday = pd.Timestamp("2024-01-05")
    assert pick_symbols(df, friday, cfg) == []
    assert pick_symbols(df, pd.Timestamp("2024-01-04"), cfg) == ["AAA"]


def test_ranks_by_trailing_overnight_momentum():
    n = 30
    strong = bars(np.linspace(10, 13, n) * 1.01, np.linspace(10, 13, n))  # gaps up nightly
    weak = bars(np.linspace(10, 13, n) * 0.99, np.linspace(10, 13, n))  # gaps down nightly
    flat = bars([10] * n, [10] * n)
    data = {"WEAK": weak, "FLAT": flat, "STRONG": strong}
    cfg = OvernightConfig(symbols=("WEAK", "FLAT", "STRONG"), max_positions=2)
    assert pick_symbols(data, strong.index[25], cfg) == ["STRONG", "FLAT"]


def test_momentum_uses_only_completed_nights():
    # A huge gap after day 25 must not influence the pick made on day 25.
    n = 30
    a = bars([10] * n, [10] * n)
    b_open = [10.0] * n
    b_open[26] = 20.0
    b = bars(b_open, [10] * n)
    cfg = OvernightConfig(symbols=("A", "B"), max_positions=1)
    picks = pick_symbols({"A": a, "B": b}, a.index[25], cfg)
    assert picks == ["A"]  # tie broken by symbol order, not by tomorrow's open


def test_backtest_books_cost_and_never_exceeds_cash():
    df = bars([10, 10.5, 10.5, 10.5], [10, 10, 10, 10])
    cfg = OvernightConfig(symbols=("AAA",), alloc_pct=0.05, cost_bps=10)
    res = run_overnight_backtest({"AAA": df}, cfg, capital=100_000)
    t = res.trades
    assert len(t) == 3
    assert t["return"].iloc[0] == pytest.approx(0.05 - 0.001)
    assert t["amount"].iloc[0] == pytest.approx(5_000)
    assert t["amount"].iloc[1] == pytest.approx(0.05 * res.equity.iloc[0])
    assert res.metrics["trades"] == 3
    assert res.metrics["win_rate"] == 1.0


def test_total_exposure_capped_by_cash_reserve():
    n = 5
    syms = ("A", "B", "C", "D", "E", "F")
    data = {s: bars([10] * n, [10] * n) for s in syms}
    cfg = OvernightConfig(symbols=syms, alloc_pct=0.20, max_positions=6)
    res = run_overnight_backtest(data, cfg, capital=100_000)
    first = res.trades[res.trades["buy_date"] == data["A"].index[0]]
    assert first["amount"].sum() <= 100_000 * (1 - cfg.cash_reserve_pct) + 1e-6


def test_verdict_fails_on_a_coin_flip():
    rng = np.random.default_rng(1)
    n = 400
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    opens = np.concatenate([[closes[0]], closes[:-1] * (1 + rng.normal(0, 0.01, n - 1))])
    res = run_overnight_backtest({"AAA": bars(opens, closes)}, OvernightConfig(symbols=("AAA",)))
    assert res.metrics["trades"] >= 100
    assert res.metrics["passes_gate"] is False


# ------------------------------------------------------------ walk-forward selection


def _drift(n, nightly, start="2024-01-01"):
    """Flat closes; each open sits ``nightly`` above the previous close."""
    closes = [10.0] * n
    opens = [10.0] + [10.0 * (1 + nightly)] * (n - 1)
    return bars(opens, closes, start=start)


def test_monthly_selection_off_by_default():
    data = {s: _drift(60, 0.0) for s in ("A", "B")}
    cfg = OvernightConfig(symbols=("A", "B"))
    assert monthly_selection(data, cfg) is None


def test_monthly_selection_keeps_top_by_trailing_overnight_return():
    n = 70  # Jan to early Apr 2024
    data = {"BAD": _drift(n, -0.01), "OK": _drift(n, 0.0), "GOOD": _drift(n, 0.01)}
    cfg = OvernightConfig(symbols=("BAD", "OK", "GOOD"), select_top=1, select_lookback=10)
    sel = monthly_selection(data, cfg)
    assert sel[pd.Period("2024-02", "M")] == ("GOOD",)
    assert sel[pd.Period("2024-03", "M")] == ("GOOD",)


def test_monthly_selection_uses_only_nights_before_the_month():
    # B is flat until a huge gap on the first night of March; A gaps up slightly in Feb.
    n = 70
    a = _drift(n, 0.001)
    b = _drift(n, 0.0)
    march = b.index[b.index >= "2024-03-01"][0]
    i = b.index.get_loc(march)
    b.iloc[i + 1, b.columns.get_loc("open")] = 30.0  # night after 1 March
    cfg = OvernightConfig(symbols=("A", "B"), select_top=1, select_lookback=10)
    sel = monthly_selection({"A": a, "B": b}, cfg)
    assert sel[pd.Period("2024-03", "M")] == ("A",)


def test_monthly_selection_needs_enough_history():
    data = {s: _drift(70, 0.01) for s in ("A", "B")}
    cfg = OvernightConfig(symbols=("A", "B"), select_top=1, select_lookback=60)
    sel = monthly_selection(data, cfg)
    assert pd.Period("2024-01", "M") not in sel  # no nights before January
    assert pd.Period("2024-02", "M") not in sel  # ~22 nights < half of 60


def test_backtest_only_trades_selected_symbols():
    n = 70
    data = {"BAD": _drift(n, -0.01), "GOOD": _drift(n, 0.01)}
    cfg = OvernightConfig(
        symbols=("BAD", "GOOD"), max_positions=2, select_top=1, select_lookback=10
    )
    res = run_overnight_backtest(data, cfg)
    assert set(res.trades["symbol"]) == {"GOOD"}
    assert res.trades["buy_date"].min() >= pd.Timestamp("2024-02-01")


# ------------------------------------------------------------ shadow log (no orders)


def _empty_log():
    return pd.DataFrame(columns=SHADOW_COLUMNS)


def test_record_shadow_logs_picks_at_the_close_once():
    df = bars([10] * 5, [10, 10, 10, 10, 12])
    data = {"AAA": df}
    cfg = OvernightConfig(symbols=("AAA",))
    day = df.index[-1]
    log = record_shadow(_empty_log(), data, cfg, "test", day)
    assert len(log) == 1
    row = log.iloc[0]
    assert (row["variant"], row["symbol"], row["buy_close"]) == ("test", "AAA", 12.0)
    assert pd.isna(row["sell_open"])
    again = record_shadow(log, data, cfg, "test", day)
    assert len(again) == 1  # idempotent per day and variant


def test_record_shadow_skips_days_without_a_bar():
    df = bars([10] * 5, [10] * 5)
    cfg = OvernightConfig(symbols=("AAA",))
    holiday = df.index[-1] + pd.Timedelta(days=1)
    assert record_shadow(_empty_log(), {"AAA": df}, cfg, "test", holiday).empty


def test_settle_shadow_fills_next_open_after_cost():
    df = bars([10, 10, 10, 11], [10, 10, 10, 10])
    cfg = OvernightConfig(symbols=("AAA",), cost_bps=10)
    log = record_shadow(_empty_log(), {"AAA": df.iloc[:3]}, cfg, "test", df.index[2])
    pending = settle_shadow(log, {"AAA": df.iloc[:3]}, cfg)
    assert pd.isna(pending.iloc[0]["sell_open"])  # next open not known yet
    done = settle_shadow(log, {"AAA": df}, cfg)
    row = done.iloc[0]
    assert row["sell_date"] == df.index[3]
    assert row["sell_open"] == 11.0
    assert row["return"] == pytest.approx(11 / 10 - 1 - 0.001)


def test_shadow_summary_counts_settled_nights_only():
    log = pd.DataFrame(
        [
            {"variant": "v", "buy_date": "2026-01-02", "symbol": "A", "buy_close": 10,
             "sell_date": "2026-01-05", "sell_open": 11, "return": 0.1},
            {"variant": "v", "buy_date": "2026-01-05", "symbol": "A", "buy_close": 10,
             "sell_date": "2026-01-06", "sell_open": 9, "return": -0.1},
            {"variant": "v", "buy_date": "2026-01-06", "symbol": "A", "buy_close": 10,
             "sell_date": None, "sell_open": None, "return": None},
        ],
        columns=SHADOW_COLUMNS,
    )  # fmt: skip
    s = shadow_summary(log, OvernightConfig(symbols=("A",)))
    v = s.loc["v"]
    assert v["trades"] == 2 and v["pending"] == 1
    assert v["win_rate"] == 0.5
    # 5% of $100k each night: +$500 then -5% of 5% of $100,500
    assert v["pnl_on_100k"] == pytest.approx(500 - 0.05 * 100_500 * 0.1)
