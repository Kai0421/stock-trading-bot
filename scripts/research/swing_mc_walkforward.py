# Author: Kai0421
"""Walk-forward and Monte Carlo views of the live swing strategy's backtest (research only).

Runs the backtest the bot's rules describe (swing mode, the live strategy, the walk-forward
win-rate gate on) and writes two separate reports:
  * walk-forward: the same fixed rules judged period by period, with the luck test;
  * Monte Carlo: the same trades resampled thousands of times to show how lucky or unlucky they
    could have been (it cannot create an edge).
No orders, no broker writes.

    uv run python scripts/research/swing_mc_walkforward.py --env LAB/.env --cache DIR
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from bot import luck_test as lt
from bot.backtest import BENCHMARK, BacktestConfig, fetch_start, run_backtest
from bot.config import Settings, load_env_file
from bot.data import BarCache, load_daily_bars, make_data_client
from bot.strategies import get_strategy, strategy_config
from bot.universe import universe_for

IDEAS_TRIED = 29  # docs/26-ideas-tested.md
START_EQUITY = 100_000.0


def trades_frame(trades) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "exit": [pd.Timestamp(t.exit_date) for t in trades],
            "r": [t.r_multiple for t in trades],
            "pnl": [t.pnl for t in trades],
        }
    ).sort_values("exit")


def line(label: str, d: pd.DataFrame) -> str:
    r = lt.mean_test(d["r"])
    win = (d["pnl"] > 0).mean() if len(d) else float("nan")
    return (
        f"| {label} | {len(d)} | {win:.1%} | {r.mean:+.3f}R | "
        f"{r.low:+.3f}R to {r.high:+.3f}R | {r.t:+.2f} | {r.p:.3f} |"
    )


def walk_forward_report(intro: str, d: pd.DataFrame) -> str:
    mid = pd.Timestamp("2021-01-01")
    lines = [
        "# Swing strategy: walk-forward",
        "",
        intro,
        "",
        "The rules are fixed (nothing is fitted), so walk-forward here means judging each period "
        "on its own. A real edge shows up in most periods, not just one lucky stretch.",
        "",
        "| Period | Trades | Win | Mean R | 95% range | t | p |",
        "|---|---|---|---|---|---|---|",
        line("Whole period", d),
        line("2016-2020 (first half)", d[d["exit"] < mid]),
        line("2021 on (second half)", d[d["exit"] >= mid]),
    ]
    years = sorted(d["exit"].dt.year.unique())
    lines += [line(str(y), d[d["exit"].dt.year == y]) for y in years]
    positive = sum(1 for y in years if d[d["exit"].dt.year == y]["r"].mean() > 0)
    lines += [
        "",
        f"Years with a positive average: {positive} of {len(years)}.",
        lt.sentence(lt.mean_test(d["r"]), "per trade (in R)", IDEAS_TRIED),
    ]
    return "\n".join(lines) + "\n"


def monte_carlo_report(intro: str, d: pd.DataFrame, sims: int) -> str:
    mc = lt.monte_carlo(list(d["pnl"] / START_EQUITY), sims=sims, seed=7)
    lines = [
        "# Swing strategy: Monte Carlo",
        "",
        intro,
        "",
        f"{sims:,} reshuffles of these {len(d)} trades, drawn at random with replacement. "
        "Each run adds up the dollar results as a share of the $100,000 start (no "
        "compounding). It shows how lucky or unlucky the same trades could have been; it "
        "cannot create an edge.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Actual total over the period | {d['pnl'].sum() / START_EQUITY:+.1%} |",
        f"| Median run | {mc['median_total']:+.1%} |",
        f"| Bad luck (5th percentile run) | {mc['p5_total']:+.1%} |",
        f"| Good luck (95th percentile run) | {mc['p95_total']:+.1%} |",
        f"| Runs that ended below zero | {mc['prob_loss']:.0%} |",
        f"| Typical worst fall (median) | {mc['median_max_dd']:.1%} |",
        f"| Bad-luck worst fall (95th percentile) | {mc['p95_max_dd']:.1%} |",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", type=Path, required=True)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 4))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 9))
    ap.add_argument("--sims", type=int, default=10_000)
    ap.add_argument(
        "--resume-after-days",
        type=int,
        default=20,
        help="measurement only: restart N trading days after a drawdown pause at half size "
        "(0 = the live rule, the pause is permanent)",
    )
    ap.add_argument("--strategy", default="trend_pullback", help="the live swing strategy")
    ap.add_argument("--only", choices=["walkforward", "montecarlo"], help="write just one report")
    ap.add_argument("--out-dir", type=Path, default=Path("reports/research"))
    args = ap.parse_args(argv)

    env = {**load_env_file(args.env), "TRADING_MODE": "swing", **os.environ}
    settings = Settings.from_env(env)
    members = universe_for(settings.universe_name)
    universe = sorted({*members, BENCHMARK})
    loaded = load_daily_bars(
        make_data_client(settings),
        universe,
        fetch_start(args.start, None),
        args.end,
        BarCache(str(args.cache)),
    )
    strat = get_strategy(args.strategy, strategy_config(args.strategy, settings))
    bt = BacktestConfig(
        args.start, args.end, START_EQUITY, gated=True, resume_after_days=args.resume_after_days
    )
    sectors = {s: members.get(s, "") for s in loaded.bars}
    result = run_backtest(loaded.bars, sectors, strat, settings.risk, bt)
    d = trades_frame(result.portfolio.trades)
    if d.empty:
        print("no trades")
        return 1
    d = d[d["exit"] <= pd.Timestamp(args.end) + timedelta(days=1)]
    years = max((d["exit"].max() - d["exit"].min()).days / 365.25, 0.1)
    pause_note = (
        f"restarted {args.resume_after_days} trading days after each at half size (measurement "
        "only, the live rule waits for the owner)"
        if args.resume_after_days
        else "permanent, as live, so trading stops after the first one"
    )
    intro = (
        f"Strategy `{args.strategy}`, {settings.universe_name} universe, {len(loaded.bars)} "
        f"symbols, {args.start} to {args.end}, the bot's own risk rules and a walk-forward "
        f"win-rate gate. {len(d)} trades over about {years:.1f} years. R is the result as a "
        "multiple of the risk taken. Today's index members are used, so survivorship flatters "
        f"these numbers. Drawdown pauses: {len(result.portfolio.pauses)}, {pause_note}. "
        f"Backtest headline: win rate {result.metrics.win_rate:.1%}, expectancy "
        f"{result.metrics.expectancy_r:+.3f}R, CAGR {result.metrics.cagr:+.1%}, max drawdown "
        f"{result.metrics.max_drawdown:.1%}."
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = "2026-10-19"
    if args.only != "montecarlo":
        text = walk_forward_report(intro, d)
        (args.out_dir / f"swing-walkforward-{stamp}.md").write_text(text, encoding="utf-8")
        print(text)
    if args.only != "walkforward":
        text = monte_carlo_report(intro, d, args.sims)
        (args.out_dir / f"swing-montecarlo-{stamp}.md").write_text(text, encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
