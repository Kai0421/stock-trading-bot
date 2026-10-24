# Author: Kai0421
"""Research only: would a profit target or a breakeven lock have helped the live swing strategy?

Runs the same walk-forward-gated swing backtest as swing_mc_walkforward.py once per exit variant:
  * baseline: the live exit (decided at the close, sold next open);
  * target Nr: a resting sell order at entry + N x risk, filled at that price when the day's high
    reaches it (the stop is checked first, so a day that hits both counts as the stop);
  * breakeven lock after Nr: once the high has reached entry + N x risk the stop moves up to the
    entry price from the next day.
Fills use the same slippage as the backtest. No orders, no broker writes.

    uv run python scripts/research/exit_target_test.py --env LAB/.env --cache DIR
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from bot import backtest as bt_mod
from bot import luck_test as lt
from bot.backtest import BENCHMARK, BacktestConfig, fetch_start, run_backtest
from bot.config import Settings, load_env_file
from bot.data import BarCache, load_daily_bars, make_data_client
from bot.strategies import get_strategy, strategy_config
from bot.universe import universe_for

START_EQUITY = 100_000.0
BASE_OPEN_TRADE = bt_mod.OpenTrade


def variant(target_r: float | None, lock_r: float | None):
    class Variant(BASE_OPEN_TRADE):  # type: ignore[valid-type, misc]
        def on_close(self, bar, strategy) -> None:  # noqa: ANN001
            if self.closed:
                return
            risk = self.entry_price - self.initial_stop
            high = float(bar["high"])
            if target_r is not None and high >= self.entry_price + target_r * risk:
                self._sell(self.shares, self.entry_price + target_r * risk, "target")
                return
            super().on_close(bar, strategy)
            if lock_r is not None and high >= self.entry_price + lock_r * risk:
                self.stop = max(self.stop, self.entry_price)

    return Variant


def summarise(label: str, trades) -> str:
    r = pd.Series([t.r_multiple for t in trades])
    pnl = pd.Series([t.pnl for t in trades])
    if r.empty:
        return f"| {label} | 0 | | | | |"
    res = lt.mean_test(r)
    return (
        f"| {label} | {len(r)} | {(pnl > 0).mean():.1%} | {res.mean:+.3f}R | "
        f"{res.low:+.3f}R to {res.high:+.3f}R | {res.t:+.2f} |"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", type=Path, required=True)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 4))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 9))
    ap.add_argument("--strategy", default="trend_pullback")
    ap.add_argument("--out", type=Path, default=Path("reports/research/exit-target-test.md"))
    args = ap.parse_args(argv)

    env = {**load_env_file(args.env), "TRADING_MODE": "swing", **os.environ}
    settings = Settings.from_env(env)
    members = universe_for(settings.universe_name)
    loaded = load_daily_bars(
        make_data_client(settings),
        sorted({*members, BENCHMARK}),
        fetch_start(args.start, None),
        args.end,
        BarCache(str(args.cache)),
    )
    strat = get_strategy(args.strategy, strategy_config(args.strategy, settings))
    cfg = BacktestConfig(args.start, args.end, START_EQUITY, gated=True, resume_after_days=20)
    sectors = {s: members.get(s, "") for s in loaded.bars}

    variants = [("live exit (baseline)", None, None)]
    variants += [(f"sell at +{n}R target", n, None) for n in (0.5, 0.75, 1.0, 1.5)]
    variants += [(f"stop to breakeven after +{n}R", None, n) for n in (0.5, 0.75, 1.0)]
    rows = []
    by_year: dict[str, pd.Series] = {}
    for label, tr, lr in variants:
        bt_mod.OpenTrade = variant(tr, lr) if (tr or lr) else BASE_OPEN_TRADE
        result = run_backtest(loaded.bars, sectors, strat, settings.risk, cfg)
        trades = [
            t
            for t in result.portfolio.trades
            if t.exit_date <= pd.Timestamp(args.end) + timedelta(days=1)
        ]
        rows.append(summarise(label, trades))
        print(rows[-1], flush=True)
        by_year[label] = (
            pd.Series([t.r_multiple for t in trades], index=[t.exit_date.year for t in trades])
            .groupby(level=0)
            .mean()
        )
    bt_mod.OpenTrade = BASE_OPEN_TRADE

    head = (
        "# Profit target and breakeven lock vs the live swing exit\n\n"
        f"Backtest {args.start} to {args.end}, strategy {args.strategy}, same walk-forward "
        "gate and costs for every row. A row only counts as an edge if it clears the stricter "
        "bar in "
        "docs/26 (t about 3.1 or more), not just beats the baseline.\n\n"
        "| Exit rule | Trades | Win rate | Mean per trade | 95% range | t |\n"
        "|---|---|---|---|---|---|\n"
    )
    yearly = pd.DataFrame(by_year).round(3)
    rows_y = [
        "",
        "Average per trade (R) by exit year:",
        "",
        "| Year | " + " | ".join(yearly.columns) + " |",
        "|---|" + "---|" * len(yearly.columns),
    ]
    rows_y += [
        f"| {y} | " + " | ".join(f"{v:+.3f}" if v == v else "" for v in r) + " |"
        for y, r in yearly.iterrows()
    ]
    base, best = yearly["live exit (baseline)"], yearly["sell at +1.5R target"]
    wins = int((best > base).sum())
    rows_y += ["", f"+1.5R beat the live exit in {wins} of {len(yearly)} years."]
    rows = rows + rows_y
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
