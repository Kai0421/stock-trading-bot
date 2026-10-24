# Author: Kai0421
"""One-off research (cached daily bars only, no orders): does the overnight hold work when
the stocks are chosen walk-forward from the bot's universe instead of a hand-picked chip
list? Run from the repo root:  uv run python scripts/research/overnight_walkforward.py

Each month the top N stocks by average overnight return over the previous year (nights
that ended before that month) form that month's list; the usual nightly rules then pick
up to 3 of them. Prints a table and writes data/research/overnight_walkforward.csv.
"Paper" columns are a simulated $100,000 account over the last 12 months, not orders.
"""

import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from bot.data import BarCache
from bot.overnight_hold import CHIP_BASKET, NIGHT_FILTERS, OvernightConfig, run_overnight_backtest
from bot.universe import symbols as universe_symbols

START = date(2016, 1, 1)
PAPER_MONTHS = 12
OUT = Path("data/research/overnight_walkforward.csv")


def load(syms: list[str]) -> dict[str, pd.DataFrame]:
    cache = BarCache()
    bars = {}
    for s in syms:
        df = cache.read(s)
        if df is not None and len(df) > 300:
            bars[s] = df.loc[pd.Timestamp(START) :]
    return bars


def row(name: str, res, paper_start: pd.Timestamp) -> dict:
    m = res.metrics
    eq = res.equity
    paper = eq[eq.index >= paper_start]
    before = eq[eq.index < paper_start]
    base = float(before.iloc[-1]) if len(before) else 100_000.0
    t = res.trades[res.trades["buy_date"] >= paper_start]
    return {
        "variant": name,
        "trades": m["trades"],
        "win_rate": round(m["win_rate"], 4),
        "lower_95": round(m["win_rate_lower_95"], 4),
        "avg_return_bps": round(m["avg_return"] * 10_000, 2),
        "cagr": round(m["cagr"], 4),
        "max_drawdown": round(m["max_drawdown"], 4),
        "passes_60_gate": m["passes_gate"],
        "paper_trades": len(t),
        "paper_win_rate": round(float((t["return"] > 0).mean()), 4) if len(t) else 0.0,
        "paper_pnl_on_100k": round((float(paper.iloc[-1]) / base - 1) * 100_000, 0)
        if len(paper)
        else 0.0,
        "paper_max_drawdown": round(float((paper / paper.cummax() - 1).min()), 4)
        if len(paper)
        else 0.0,
    }


def main() -> int:
    universe = sorted(set(universe_symbols()) | set(CHIP_BASKET))
    bars = load(universe)
    if not bars:
        print("no cached bars under data/bars; run a backtest first", file=sys.stderr)
        return 1
    last = max(b.index[-1] for b in bars.values())
    paper_start = (last - pd.DateOffset(months=PAPER_MONTHS)).normalize()
    print(f"{len(bars)} symbols, {START} to {last.date()}, paper period from {paper_start.date()}")

    pool = tuple(s for s in universe if s in bars)
    chips = tuple(s for s in CHIP_BASKET if s in bars)
    variants: list[tuple[str, OvernightConfig]] = []
    for f in sorted(NIGHT_FILTERS):
        variants.append((f"chips fixed, {f}", OvernightConfig(symbols=chips, night_filter=f)))
        for top in (9, 20):
            cfg = OvernightConfig(symbols=pool, night_filter=f, select_top=top)
            variants.append((f"universe top{top} walk-forward, {f}", cfg))
    # Cost sensitivity on the default nights.
    base = OvernightConfig(symbols=pool, select_top=9)
    variants.append(("universe top9 walk-forward, all, 20 bps", replace(base, cost_bps=20)))

    rows = []
    for name, cfg in variants:
        rows.append(row(name, run_overnight_backtest(bars, cfg), paper_start))
        print(".", end="", flush=True)
    print()
    table = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT, index=False)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(table.to_string(index=False))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
