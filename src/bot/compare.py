# Author: Kai0421
"""Compare strategies side by side on the same bars, costs, gate and risk rules.

Each strategy gets the full walk-forward backtest (``bot.backtest.run_backtest``); the
report puts their out-of-sample portfolio metrics and acceptance results in one table.
Comparing writes a report only: it never writes the evidence file the live gate reads,
so it can't change what the bot trades. To trade a strategy, backtest it with
``python -m bot.backtest --strategy NAME`` and set ``STRATEGY=NAME``.

Run: ``uv run python -m bot.compare --start 2016-01-01 --end 2026-09-30``
(all registered strategies) or ``--strategies trend_pullback,rsi2_reversion``.
Reports go to ``memory/backtests/compare-<date>-<hash>.md``.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from bot.backtest import (
    DEFAULT_REPORT_DIR,
    ET,
    BacktestConfig,
    BacktestResult,
    _fmt,
    _pauses_text,
    fetch_start,
    run_backtest,
    write_report,
)
from bot.config import ConfigError, RiskConfig, Settings, load_env_file
from bot.strategies import Strategy, get_strategy, registry, strategy_config
from bot.universe import BENCHMARK


def compare_strategies(
    bars: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str],
    strategies: Sequence[Strategy],
    risk: RiskConfig,
    bt: BacktestConfig,
) -> list[tuple[Strategy, BacktestResult]]:
    """Backtest every strategy on exactly the same inputs."""
    names = [s.name for s in strategies]
    if len(set(names)) != len(names):
        raise ValueError(f"each strategy may appear once (got {names})")
    return [(s, run_backtest(bars, sectors, s, risk, bt)) for s in strategies]


def _nan_last(x: float) -> float:
    return x if x == x else -1e9


@dataclasses.dataclass(frozen=True)
class Suggestion:
    name: str | None  # the strategy to trade, or None when no candidate passes
    switch: bool  # True when it differs from the strategy the bot trades now
    text: str


def suggest(results: Sequence[tuple[Strategy, BacktestResult]], current: str) -> Suggestion:
    """The candidate to trade: passes acceptance, then highest expectancy, then highest
    win-rate lower bound. Advice only: switching still needs the backtest with
    ``--stability``, a memory/decisions.md entry and ``STRATEGY=<name>``."""

    def key(sr: tuple[Strategy, BacktestResult]) -> tuple[float, float]:
        m = sr[1].metrics
        return (_nan_last(m.expectancy_r), _nan_last(m.wilson_lb))

    passing = [sr for sr in results if not sr[1].failures]
    if not passing:
        return Suggestion(
            None,
            False,
            f"No candidate passes acceptance: keep `{current}`. If in doubt, don't trade.",
        )
    best, r = max(passing, key=key)
    m = r.metrics
    stats = f"expectancy {_fmt(m.expectancy_r, 'r')}, win-rate lower bound {_fmt(m.wilson_lb)}"
    if best.name == current:
        return Suggestion(
            best.name, False, f"Keep `{current}`: it is the best candidate ({stats})."
        )
    return Suggestion(
        best.name,
        True,
        f"Consider `{best.name}` ({stats}) instead of `{current}`. Before switching: "
        f"`python -m bot.backtest --strategy {best.name} --stability` on a held-out period, "
        f"a memory/decisions.md entry, then `STRATEGY={best.name}` with no open positions.",
    )


def _rank_key(result: BacktestResult) -> tuple:
    m = result.metrics
    expectancy = m.expectancy_r if m.expectancy_r == m.expectancy_r else -1e9  # NaN last
    return (bool(result.failures), -expectancy)


@dataclasses.dataclass(frozen=True)
class BuyAndHold:
    total_return: float
    cagr: float
    max_drawdown: float

    @property
    def text(self) -> str:
        return (
            f"{BENCHMARK} buy and hold: {self.total_return:+.1%} total, "
            f"{self.cagr:+.1%} a year, max drawdown {self.max_drawdown:.1%}"
        )


def buy_and_hold(spy: pd.DataFrame, bt: BacktestConfig) -> BuyAndHold:
    """The benchmark: SPY bought at the first close in the period and held to the last."""
    close = spy["close"].loc[pd.Timestamp(bt.start) : pd.Timestamp(bt.end)].astype(float)
    if len(close) < 2:
        raise ValueError(f"not enough {BENCHMARK} bars in the period")
    growth = close / close.iloc[0]
    years = (close.index[-1] - close.index[0]).days / 365.25
    total = float(growth.iloc[-1] - 1)
    cagr = float(growth.iloc[-1] ** (1 / years) - 1) if years > 0 else float("nan")
    drawdown = float((1 - growth / growth.cummax()).max())
    return BuyAndHold(total, cagr, drawdown)


def render_comparison(
    results: Sequence[tuple[Strategy, BacktestResult]],
    risk: RiskConfig,
    bt: BacktestConfig,
    run_id: str,
    notes: Sequence[str] = (),
    current: str | None = None,
    benchmark: BuyAndHold | None = None,
) -> str:
    """Markdown report: one row per strategy, passing strategies first, then by expectancy."""
    ordered = sorted(results, key=lambda sr: _rank_key(sr[1]))
    lines = [
        f"# Strategy comparison {run_id}",
        "",
        f"- Period: {bt.start} to {bt.end}; initial equity ${bt.initial_equity:,.0f}",
        f"- Probability gate: {'on (walk-forward, expanding window)' if bt.gated else 'OFF'}"
        f"; GATE_MODE={risk.gate_mode}",
        "- Drawdown pause: "
        + (
            f"trading restarts {bt.resume_after_days} trading days after a pause, at "
            f"{bt.resume_size:.0%} size until the old peak is regained (measurement only; "
            "the live bot still waits for the owner)"
            if bt.resume_after_days
            else "permanent (as live: no new entries after the first pause)"
        ),
        f"- Sizing: risk {risk.effective_risk_per_trade:.2%} per trade, at most "
        f"{risk.max_open_positions} positions, {risk.max_position_pct:.0%} of equity each",
        f"- Costs: {risk.slippage_bps_round_trip:g} bps round-trip slippage, "
        f"${bt.fees_per_order:g} fees per order",
        "- Same bars, fills, gate and risk rules for every strategy. Acceptance as in "
        "docs/03 (no stability check here; run `python -m bot.backtest --strategy NAME "
        "--stability` on a candidate before trading it).",
    ]
    if benchmark is not None:
        lines.append(f"- Benchmark: {benchmark.text}")
    lines.append("")
    if current is not None:
        lines += ["## Suggestion", "", suggest(results, current).text, ""]
    lines += [
        "## Portfolio trades (out-of-sample)",
        "",
        "| Strategy | Accepted | Trades | Win rate | Lower bound | Expectancy | CAGR "
        "| Max DD | Sharpe | Exposure | Pauses |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s, r in ordered:
        m = r.metrics
        lines.append(
            f"| `{s.name}` | {'yes' if not r.failures else 'no'} | {m.trades} "
            f"| {_fmt(m.win_rate)} | {_fmt(m.wilson_lb)} | {_fmt(m.expectancy_r, 'r')} "
            f"| {_fmt(m.cagr)} | {_fmt(m.max_drawdown)} | {_fmt(m.sharpe, 'num')} "
            f"| {_fmt(m.exposure)} | {len(r.portfolio.pauses)} |"
        )
    lines += [
        "",
        "## Signal-level evidence (every setup, ungated)",
        "",
        "| Strategy | Setups traded | Win rate | Lower bound | Expectancy |",
        "|---|---|---|---|---|",
    ]
    for s, r in ordered:
        m = r.signal_metrics
        lines.append(
            f"| `{s.name}` | {m.trades} | {_fmt(m.win_rate)} | {_fmt(m.wilson_lb)} "
            f"| {_fmt(m.expectancy_r, 'r')} |"
        )
    paused = [(s, r) for s, r in ordered if r.portfolio.pauses]
    if paused:
        lines += ["", "## Drawdown pauses", ""]
        lines += [f"- `{s.name}`: {_pauses_text(r.portfolio)}" for s, r in paused]
    lines += ["", "## Acceptance failures", ""]
    for s, r in ordered:
        lines.append(f"- `{s.name}`: " + ("; ".join(r.failures) or "none"))
    lines += ["", "## Parameters", ""]
    for s, _ in ordered:
        lines += [
            f"### `{s.name}`",
            "",
            s.description,
            "",
            "```json",
            json.dumps(dataclasses.asdict(s.config), indent=2),
            "```",
            "",
        ]
    lines += [
        "## Caveats",
        "",
        "- Survivorship bias: the universe is today's index members. Results are likely "
        "optimistic.",
        "- Picking the best of several backtests overfits; treat the winner as a hypothesis "
        "and confirm it on a held-out period (`--history-start`) before trading it.",
        "- A probability is a historical estimate, not a guarantee.",
    ]
    if notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"


def equity_csv(results: Sequence[tuple[Strategy, BacktestResult]], gate_mode: str) -> str:
    """Month-end portfolio equity per strategy, for charting runs side by side."""
    lines = ["gate_mode,strategy,month,equity"]
    for s, r in results:
        monthly = r.portfolio.equity.groupby(r.portfolio.equity.index.to_period("M")).last()
        lines += [f"{gate_mode},{s.name},{m},{v:.0f}" for m, v in monthly.items()]
    return "\n".join(lines) + "\n"


def compare_id(
    strategies: Sequence[Strategy], risk: RiskConfig, bt: BacktestConfig, today: date
) -> str:
    configs = [[s.name, dataclasses.asdict(s.config)] for s in strategies]
    payload = json.dumps(
        [configs, dataclasses.asdict(risk), dataclasses.asdict(bt)],
        default=str,
        sort_keys=True,
    )
    return f"compare-{today:%Y%m%d}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    from bot.data import BarCache, load_daily_bars, make_data_client
    from bot.universe import UNIVERSE_NAMES, universe_for

    ap = argparse.ArgumentParser(prog="python -m bot.compare", description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument(
        "--strategies",
        help="comma-separated registry names (default: STRATEGY_CANDIDATES, else all: "
        f"{', '.join(sorted(registry()))})",
    )
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--history-start", type=date.fromisoformat)
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--symbols", help="comma-separated subset of the universe")
    ap.add_argument(
        "--universe",
        choices=sorted(UNIVERSE_NAMES),
        help="symbol universe (default: the UNIVERSE setting, sp100)",
    )
    ap.add_argument("--no-gate", action="store_true", help="diagnostics: take every setup")
    ap.add_argument(
        "--resume-after-days",
        type=int,
        default=0,
        help="restart N trading days after a drawdown pause at half size (0 = never)",
    )
    ap.add_argument("--cache", default="data/bars")
    ap.add_argument("--out", default=str(DEFAULT_REPORT_DIR))
    args = ap.parse_args(argv)

    if env is None:
        env = {**load_env_file(Path(".env")), **os.environ}
    try:
        settings = Settings.from_env(env)
        if args.strategies:
            names = args.strategies.split(",")
        else:
            names = list(settings.strategy_candidates) or sorted(registry())
        strategies = [get_strategy(n.strip(), strategy_config(n.strip(), settings)) for n in names]
        first_bar = fetch_start(args.start, args.history_start)
        client = make_data_client(settings)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    universe_name = args.universe or settings.universe_name
    members = universe_for(universe_name)
    universe = args.symbols.split(",") if args.symbols else sorted(members)
    if BENCHMARK not in universe:
        universe = [BENCHMARK, *universe]
    loaded = load_daily_bars(
        client, universe, first_bar, args.end, BarCache(args.cache), feed=args.feed
    )
    if BENCHMARK not in loaded.bars:
        print(f"error: no {BENCHMARK} bars: {loaded.skipped.get(BENCHMARK)}", file=sys.stderr)
        return 2

    risk = settings.risk
    try:
        bt = BacktestConfig(
            args.start,
            args.end,
            args.equity,
            gated=not args.no_gate,
            resume_after_days=args.resume_after_days,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sectors = {s: members.get(s, "") for s in loaded.bars}
    try:
        results = compare_strategies(loaded.bars, sectors, strategies, risk, bt)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    notes = [f"skipped {s}: {why}" for s, why in sorted(loaded.skipped.items())]
    notes.append(
        f"{len(loaded.bars)} symbols ({universe_name}), {args.feed.upper()} feed, "
        f"history from {first_bar}"
    )
    run_id = compare_id(strategies, risk, bt, datetime.now(ET).date())
    if universe_name != "sp100":
        run_id += f"-{universe_name}"
    report = render_comparison(
        results,
        risk,
        bt,
        run_id,
        notes,
        current=settings.strategy_name,
        benchmark=buy_and_hold(loaded.bars[BENCHMARK], bt),
    )
    path = write_report(report, run_id, args.out)
    Path(path).with_suffix(".equity.csv").write_text(equity_csv(results, risk.gate_mode))
    for s, r in sorted(results, key=lambda sr: _rank_key(sr[1])):
        verdict = "passes acceptance" if not r.failures else "fails acceptance"
        print(f"{s.name}: {r.metrics.trades} trades, {verdict}")
    print(f"suggestion: {suggest(results, settings.strategy_name).text}")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
