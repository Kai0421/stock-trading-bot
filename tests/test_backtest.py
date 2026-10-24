# Author: Kai0421
"""Tests for bot.backtest: fills, signal trades, point-in-time gate, portfolio, metrics."""

import dataclasses
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest import (
    BacktestConfig,
    Metrics,
    OpenTrade,
    SignatureBook,
    Trade,
    acceptance,
    compute_metrics,
    main,
    perturbations,
    prepare,
    render_report,
    run_backtest,
    run_portfolio,
    simulate_signals,
    stability_check,
    write_report,
)
from bot.config import RiskConfig, StrategyConfig
from bot.risk import GateResult

STRAT = StrategyConfig(exit_set="trend", time_stop_days=15)
RISK = RiskConfig()
SLIP = RISK.slippage_bps_round_trip / 2 / 10_000  # 5 bps per side


# ------------------------------------------------------- synthetic market


def ohlcv(closes, idx, spread=0.008, volume=2_000_000):
    c = np.asarray(closes, dtype="float64")
    opens = np.concatenate([[c[0]], c[:-1]])  # open at the previous close
    high = np.maximum(opens, c) * (1 + spread)
    low = np.minimum(opens, c) * (1 - spread)
    return pd.DataFrame(
        {"open": opens, "high": high, "low": low, "close": c, "volume": volume}, index=idx
    )


def market(n=700, n_symbols=6, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    bars = {"SPY": ohlcv(100 * np.cumprod(1 + rng.normal(0.0006, 0.008, n)), idx, 0.004)}
    sectors = {"SPY": "Broad"}
    for k in range(n_symbols):
        bars[f"S{k}"] = ohlcv(100 * np.cumprod(1 + rng.normal(0.0012, 0.016, n)), idx)
        sectors[f"S{k}"] = "AB"[k % 2] if k < 4 else f"C{k}"
    return bars, sectors


BT = BacktestConfig(start=date(2018, 1, 1), end=date(2020, 12, 31), gated=False)


class AlwaysPass:
    def gate(self, signature, as_of, cfg):
        return GateResult(True, "passed", 0.8, 0.7)


class AlwaysFail:
    def gate(self, signature, as_of, cfg):
        return GateResult(False, "no", 0.5, 0.4)


# ------------------------------------------------------- OpenTrade fills


def trade(**kw):
    base = dict(
        symbol="X",
        signature="sig",
        signal_date=pd.Timestamp("2020-01-02"),
        entry_date=pd.Timestamp("2020-01-03"),
        entry_price=100.0,
        initial_stop=96.0,
        shares=100,
        slippage=SLIP,
    )
    base.update(kw)
    return OpenTrade(**base)


def test_intraday_stop_fills_at_stop_less_slippage():
    t = trade()
    assert t.on_open(99.0) == 0.0
    proceeds = t.on_low(95.0)
    assert proceeds == pytest.approx(100 * 96 * (1 - SLIP))
    assert t.closed
    tr = t.to_trade(pd.Timestamp("2020-01-03"))
    assert tr.exit_reason == "stop"
    assert tr.pnl == pytest.approx(100 * 96 * (1 - SLIP) - 10_000)
    assert tr.r_multiple == pytest.approx(tr.pnl / 400)
    assert not tr.win


def test_gap_through_stop_fills_at_open():
    t = trade()
    t.on_open(94.0)
    assert t.closed
    assert t.to_trade(pd.Timestamp("2020-01-06")).pnl == pytest.approx(
        100 * 94 * (1 - SLIP) - 10_000
    )


def test_trend_break_exits_next_open():
    t = trade()
    t.on_close(dict(close=99.0, atr=2.0, ema=99.0, sma_fast=100.0, sma_exit=99.0), STRAT)
    assert not t.closed
    t.on_open(98.0)
    assert t.closed
    assert t.to_trade(pd.Timestamp("2020-01-06")).exit_reason == "trend_break"


def test_partial_then_trailing_stop():
    t = trade()
    t.on_close(dict(close=108.0, atr=2.0, ema=105.0, sma_fast=100.0, sma_exit=106.0), STRAT)
    assert t.stop == pytest.approx(104.8)  # max(entry, 108 - 6, 105 - 0.2)
    t.on_open(109.0)  # sells 50 at the open
    assert t.shares == 50 and t.partial_taken
    t.on_low(104.0)  # trailing stop at 104.8 hit
    tr = t.to_trade(pd.Timestamp("2020-01-07"))
    expected = 50 * 109 * (1 - SLIP) + 50 * 104.8 * (1 - SLIP) - 10_000
    assert tr.pnl == pytest.approx(expected)
    assert tr.exit_reason == "stop"
    assert tr.win


def test_fees_reduce_pnl():
    t = trade(fees=1.0)
    t.on_low(95.0)
    tr = t.to_trade(pd.Timestamp("2020-01-03"))
    assert tr.pnl == pytest.approx(100 * 96 * (1 - SLIP) - 10_000 - 2.0)


def test_close_out_at_end_of_data():
    t = trade()
    t.close_out(101.0)
    tr = t.to_trade(pd.Timestamp("2020-01-03"))
    assert tr.exit_reason == "end_of_data"
    assert tr.pnl == pytest.approx(100 * 101 * (1 - SLIP) - 10_000)


# ------------------------------------------------------- signal trades


def test_signal_trades_enter_next_open_without_overlap():
    bars, _ = market()
    features, tables = prepare(bars, STRAT)
    trades = simulate_signals("S0", features["S0"], tables["S0"], STRAT, RISK)
    assert trades, "synthetic market should produce setups"
    opens = features["S0"]["open"]
    for tr in trades:
        assert tr.entry_date > tr.signal_date
        assert tables["S0"]["setup"].loc[tr.signal_date]
        assert tr.signature == tables["S0"]["signature"].loc[tr.signal_date]
        assert tr.entry_price == pytest.approx(opens.loc[tr.entry_date] * (1 + SLIP))
        assert 0 < tr.initial_stop < tr.entry_price
        assert tr.exit_date >= tr.entry_date
    for a, b in zip(trades, trades[1:], strict=False):
        assert b.signal_date >= a.exit_date


def test_signal_trades_have_no_look_ahead():
    """Trades closed within a truncated history are identical with the full history."""
    bars, _ = market()
    full = simulate_signals("S1", *_prep_one(bars, "S1"), STRAT, RISK)
    cut = 450
    short_bars = {k: v.iloc[:cut] for k, v in bars.items()}
    short = simulate_signals("S1", *_prep_one(short_bars, "S1"), STRAT, RISK)
    closed = [t for t in short if t.exit_reason != "end_of_data"]
    assert closed
    assert closed == [t for t in full if t.exit_date <= short_bars["S1"].index[-1]][: len(closed)]


def _prep_one(bars, symbol):
    features, tables = prepare(bars, STRAT)
    return features[symbol], tables[symbol]


# ------------------------------------------------------- signature book


def _t(exit_day, win=True, sig="A", r=None):
    d = pd.Timestamp(exit_day)
    pnl = 10.0 if win else -10.0
    return Trade(
        symbol="X",
        signature=sig,
        signal_date=d,
        entry_date=d,
        exit_date=d,
        entry_price=100.0,
        initial_stop=95.0,
        shares=1,
        pnl=pnl,
        r_multiple=(pnl / 5 if r is None else r),
        exit_reason="stop",
    )


def test_book_uses_only_trades_closed_before_date():
    days = pd.bdate_range("2019-01-01", periods=120)
    trades = [_t(d, win=(i % 5 != 0)) for i, d in enumerate(days)]  # 80% wins
    book = SignatureBook(trades)
    assert book.stats("A", days[100]) == (
        100,
        80,
        pytest.approx(np.mean([t.r_multiple for t in trades[:100]])),
        pytest.approx(np.std([t.r_multiple for t in trades[:100]], ddof=1)),
    )
    assert not book.gate("A", days[99], RISK).passed  # only 99 trades closed before
    assert book.gate("A", days[100], RISK).passed
    assert not book.gate("B", days[-1], RISK).passed


def test_book_ignores_end_of_data_trades():
    days = pd.bdate_range("2019-01-01", periods=150)
    trades = [_t(d) for d in days]
    trades = [Trade(**{**t.__dict__, "exit_reason": "end_of_data"}) for t in trades]
    assert SignatureBook(trades).stats("A", days[-1] + pd.Timedelta(days=1))[0] == 0


def test_book_rejects_negative_expectancy():
    days = pd.bdate_range("2019-01-01", periods=120)
    trades = [_t(d, win=(i % 5 != 0), r=(0.1 if i % 5 else -3.0)) for i, d in enumerate(days)]
    g = SignatureBook(trades).gate("A", days[-1] + pd.Timedelta(days=1), RISK)
    assert not g.passed
    assert "expectancy" in g.reason


# ------------------------------------------------------- portfolio


def _portfolio(book, risk=RISK, bars_sectors=None, bt=BT):
    bars, sectors = bars_sectors or market()
    features, tables = prepare(bars, STRAT)
    cal = bars["SPY"].index
    return run_portfolio(features, tables, sectors, cal, book, STRAT, risk, bt)


def test_portfolio_invariants():
    res = _portfolio(AlwaysPass())
    assert res.trades
    assert res.min_cash >= -1e-6
    assert res.max_positions <= RISK.max_open_positions
    assert res.max_per_sector <= RISK.max_per_sector
    assert all(t.entry_date > t.signal_date for t in res.trades)
    assert all(t.symbol != "SPY" or t.signature for t in res.trades)
    assert res.equity.index.is_monotonic_increasing
    assert res.equity.iloc[0] == pytest.approx(BT.initial_equity, rel=0.05)


def test_portfolio_gate_blocks_everything():
    res = _portfolio(AlwaysFail())
    assert res.trades == []
    assert res.gate_rejections > 0
    assert res.equity.iloc[-1] == pytest.approx(BT.initial_equity)


def test_ungated_matches_always_pass():
    a = _portfolio(None)
    b = _portfolio(AlwaysPass())
    assert [t.symbol for t in a.trades] == [t.symbol for t in b.trades]


def test_drawdown_pause_stops_new_entries():
    res = _portfolio(AlwaysPass(), risk=RiskConfig(drawdown_kill_pct=0.001))
    assert res.paused_on is not None
    assert all(t.entry_date <= res.paused_on for t in res.trades)
    assert len(res.pauses) == 1 and res.pauses[0][1] is None  # never resumes by default


def test_drawdown_pause_can_resume_after_a_cooling_off_period():
    risk = RiskConfig(drawdown_kill_pct=0.02)
    bt = dataclasses.replace(BT, resume_after_days=20)
    res = _portfolio(AlwaysPass(), risk=risk, bt=bt)
    assert res.paused_on is not None and res.pauses[0][0] == res.paused_on
    start, end = res.pauses[0]
    assert end is not None
    # No entries while paused; entries again after the restart.
    days = res.equity.index
    assert days.get_loc(end) - days.get_loc(start) == 20
    assert not any(start < t.entry_date <= end for t in res.trades)
    assert any(t.entry_date > end for t in res.trades)
    # Entries after a restart are at reduced size until the old peak is regained.
    assert res.reduced_size_entries > 0


def test_resume_settings_are_validated():
    with pytest.raises(ValueError):
        dataclasses.replace(BT, resume_after_days=-1)
    with pytest.raises(ValueError):
        dataclasses.replace(BT, resume_size=0)


def test_portfolio_is_deterministic():
    a = _portfolio(AlwaysPass())
    b = _portfolio(AlwaysPass())
    assert a.trades == b.trades
    pd.testing.assert_series_equal(a.equity, b.equity)


# ------------------------------------------------------- metrics and acceptance


def test_equity_metrics():
    idx = pd.to_datetime(["2020-01-01", "2020-06-01", "2020-09-01", "2021-01-01"])
    eq = pd.Series([100.0, 120.0, 90.0, 130.0], index=idx)
    m = compute_metrics([], eq)
    assert m.max_drawdown == pytest.approx(0.25)
    assert m.total_return == pytest.approx(0.30)
    assert m.cagr == pytest.approx(1.30 ** (365.25 / 366) - 1)
    assert m.trades == 0
    assert math.isnan(m.win_rate)


def test_trade_metrics():
    days = pd.bdate_range("2020-01-01", periods=4)
    trades = [_t(days[0], r=2.0), _t(days[1], r=1.0), _t(days[2], win=False, r=-1.0), _t(days[3])]
    m = compute_metrics(trades, pd.Series([100.0, 101.0], index=days[:2]))
    assert (m.trades, m.wins) == (4, 3)
    assert m.win_rate == pytest.approx(0.75)
    assert m.expectancy_r == pytest.approx((2 + 1 - 1 + 2) / 4)
    assert m.avg_win_r == pytest.approx(5 / 3)
    assert m.avg_loss_r == pytest.approx(-1.0)


def _metrics(**kw):
    base = dict(
        trades=300,
        wins=225,
        win_rate=0.75,
        wilson_lb=0.70,
        expectancy_r=0.2,
        avg_win_r=0.6,
        avg_loss_r=-1.0,
        total_return=0.5,
        cagr=0.05,
        max_drawdown=0.10,
        sharpe=1.0,
        exposure=0.5,
    )
    base.update(kw)
    return Metrics(**base)


def test_acceptance_passes():
    assert acceptance(_metrics(), RISK) == []


@pytest.mark.parametrize(
    ("override", "word"),
    [
        ({"trades": 99}, "trades"),
        ({"win_rate": 0.59}, "win rate"),
        ({"wilson_lb": 0.59}, "lower bound"),
        ({"expectancy_r": 0.0}, "expectancy"),
        ({"max_drawdown": 0.20}, "drawdown"),
    ],
)
def test_acceptance_failures(override, word):
    failures = acceptance(_metrics(**override), RISK)
    assert any(word in f for f in failures)


# ------------------------------------------------------- end to end, stability, report


def test_run_backtest_end_to_end():
    bars, sectors = market()
    res = run_backtest(
        bars, sectors, STRAT, RISK, BacktestConfig(date(2018, 1, 1), date(2020, 12, 31))
    )
    assert res.signal_trades
    assert isinstance(res.metrics, Metrics)
    assert res.failures  # synthetic data cannot reach n >= 100 per signature
    assert res.portfolio.trades == []  # so the gate admits nothing


def test_run_backtest_requires_benchmark():
    bars, sectors = market()
    del bars["SPY"]
    with pytest.raises(ValueError):
        run_backtest(bars, sectors, STRAT, RISK, BT)


def test_perturbations():
    out = {label: s.config for label, s in perturbations(STRAT, 0.2)}
    assert out["sma_fast x0.8"].sma_fast == 40
    assert out["sma_fast x1.2"].sma_fast == 60
    assert out["stop_atr_mult x1.2"].stop_atr_mult == pytest.approx(2.4)
    assert out["time_stop_days x0.8"].time_stop_days == 12
    assert len(out) == 18


def test_stability_check_runs_each_perturbation():
    bars, sectors = market(n=500, n_symbols=3)
    results = stability_check(bars, sectors, STRAT, RISK, BT, params=("sma_fast",))
    assert [r.label for r in results] == ["sma_fast x0.8", "sma_fast x1.2"]
    assert all(not r.passed for r in results)  # ungated synthetic data fails acceptance


def test_report_and_write(tmp_path):
    bars, sectors = market(n=500, n_symbols=3)
    res = run_backtest(bars, sectors, STRAT, RISK, BT)
    text = render_report(res, STRAT, RISK, BT, run_id="test-run", notes=["note one"])
    assert "# Backtest test-run" in text
    assert "REJECTED" in text
    assert "survivorship" in text.lower()
    assert "note one" in text
    assert "sma_fast" in text
    sig_rows = [line for line in text.splitlines() if line.startswith("| `")]
    # Pipes inside signatures are escaped so they don't split table cells.
    assert sig_rows and all("\\|" in line for line in sig_rows)
    assert all(line.replace("\\|", "").count("|") == 6 for line in sig_rows)
    path = write_report(text, "test-run", tmp_path)
    assert path.read_text() == text
    assert path.name == "test-run.md"


def test_main_refuses_without_keys(capsys):
    assert main(["--start", "2016-01-01", "--end", "2026-09-30"], env={}) == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_evidence_for_result_uses_trades_up_to_end_date():
    from bot.backtest import evidence_for

    bars, sectors = market(n=500, n_symbols=3)
    res = run_backtest(bars, sectors, STRAT, RISK, BT)
    ev = evidence_for(res, BT, "rid", "REJECTED")
    assert ev.as_of == BT.end and ev.backtest_id == "rid" and ev.verdict == "REJECTED"
    end = pd.Timestamp(BT.end)
    expected = sum(
        1 for t in res.signal_trades if t.exit_reason != "end_of_data" and t.exit_date <= end
    )
    assert sum(s.n for s in ev.signatures.values()) == expected


def test_parse_overrides():
    from bot.backtest import apply_overrides

    cfg = apply_overrides(STRAT, ["exit_set=mean_reversion", "time_stop_days=8", "mr_target_r=0.8"])
    assert (cfg.exit_set, cfg.time_stop_days, cfg.mr_target_r) == ("mean_reversion", 8, 0.8)
    for bad in (["nope=1"], ["time_stop_days"], ["time_stop_days=x"]):
        with pytest.raises(ValueError):
            apply_overrides(STRAT, bad)


def test_mean_reversion_backtest_runs():
    import dataclasses as dc

    bars, sectors = market(n=600, n_symbols=3)
    res = run_backtest(bars, sectors, dc.replace(STRAT, exit_set="mean_reversion"), RISK, BT)
    assert res.signal_trades
    assert any(
        t.exit_reason in ("stop", "trend_break", "time_stop", "end_of_data")
        for t in res.signal_trades
    )


def test_fetch_start_defaults_to_warmup_and_accepts_history_start():
    from bot.backtest import WARMUP_CALENDAR_DAYS, fetch_start

    assert fetch_start(date(2023, 1, 1), None) == date(2023, 1, 1) - pd.Timedelta(
        days=WARMUP_CALENDAR_DAYS
    )
    assert fetch_start(date(2023, 1, 1), date(2014, 11, 1)) == date(2014, 11, 1)
    with pytest.raises(ValueError):
        fetch_start(date(2023, 1, 1), date(2023, 6, 1))


def test_verdict_with_advisory_stability():
    from bot.backtest import StabilityResult, verdict

    bars, sectors = market(n=500, n_symbols=3)
    res = run_backtest(bars, sectors, STRAT, RISK, BT)
    res.failures = []  # pretend the main run passed
    bad = [StabilityResult("x", False, res.metrics, ("fail",))]
    good = [StabilityResult("x", True, res.metrics, ())]
    assert verdict(res, good) == "ACCEPTED"
    assert verdict(res, bad) == "REJECTED (unstable)"
    assert verdict(res, bad, advisory=True) == "ACCEPTED (stability advisory)"
    assert verdict(res, None, advisory=True).startswith("PROVISIONAL")
    res.failures = ["x"]
    assert verdict(res, good, advisory=True) == "REJECTED"
