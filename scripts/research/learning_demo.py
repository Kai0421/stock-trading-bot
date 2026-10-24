# Author: Kai0421
"""Does the learning loop work? Research only: reads cached bars, no broker, no orders.

1. Synthetic proof: a planted losing pattern is found; pure noise is not.
2. Real history: the trend-pullback strategy's backtest trades (2019-2026) go through the same
   post-mortem, miner and challenger code, as a walk-forward: every quarter the loop looks only at
   trades that had already finished, and what it would have skipped is applied to the NEXT
   quarter. If learning helped, the skipped trades should have been worse than the kept ones.

    uv run python scripts/research/learning_demo.py --bars <cache dir> --out <report.md>
"""

from __future__ import annotations

import argparse
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from bot import learning as lg
from bot.backtest import BacktestConfig, run_backtest
from bot.config import RiskConfig
from bot.data import BarCache, load_daily_bars
from bot.strategies import get_strategy
from bot.universe import BENCHMARK, universe_for

DIMS = ["sector", "regime", "weekday", "atr_bucket", "gap_bucket", "signature"]


class NoClient:
    def get_stock_bars(self, *a, **k):  # pragma: no cover
        raise RuntimeError("cache miss: this research script never calls the broker")


# ----------------------------------------------------------------------------- 1. synthetic


def synthetic(n: int, seed: int, planted: tuple[str, str, float] | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "entry_date": pd.bdate_range("2023-01-02", periods=n),
        "sector": rng.choice([f"S{i}" for i in range(6)], n),
        "regime": rng.choice(["market_up", "market_down"], n),
        "weekday": rng.choice(["Mon", "Tue", "Wed", "Thu", "Fri"], n),
        "atr_bucket": rng.choice(["low", "mid", "high"], n),
    })  # fmt: skip
    p = np.full(n, 0.42)
    if planted:
        p[(df[planted[0]] == planted[1]).to_numpy()] = planted[2]
    lose = rng.random(n) < p
    df["win"] = ~lose
    df["r"] = np.where(lose, -1.0, 0.8) + rng.normal(0, 0.2, n)
    return df


def part1() -> list[str]:
    found = lg.mine(synthetic(600, 1, ("sector", "S2", 0.80)), DIMS)
    planted_hit = [x for x in found.lessons if (x.dim, x.value) == ("sector", "S2")]
    noise_lessons = sum(len(lg.mine(synthetic(500, 100 + s), DIMS).lessons) for s in range(100))
    noise_runs_with_any = sum(
        bool(lg.mine(synthetic(500, 100 + s), DIMS).lessons) for s in range(100)
    )
    out = ["## 1. Does the machinery work? (synthetic trades)", ""]
    if planted_hit:
        x = planted_hit[0]
        out.append(
            f"- Planted pattern (sector S2 loses 80% of the time vs 42% elsewhere, 600 trades): "
            f"**found** ({x.strength} evidence). {lg.lesson_text(x)}"
        )
    else:
        out.append("- Planted pattern: **missed**.")
    out.append(
        f"- Pure noise (100 different random sets of 500 trades, 20 situations each): "
        f"{noise_lessons} false lessons in total; {noise_runs_with_any} of 100 sets produced any."
    )
    return out + [""]


# ----------------------------------------------------------------------------- 2. real history


def to_common(trades, sectors, label: str) -> pd.DataFrame:
    rows = []
    for i, t in enumerate(trades):
        rows.append({
            "id": f"{label}-{i}", "source": label, "symbol": t.symbol,
            "sector": sectors.get(t.symbol, ""),
            "signature": t.signature, "entry_date": t.entry_date, "exit_date": t.exit_date,
            "entry_price": t.entry_price,
            "exit_price": t.entry_price + (t.pnl / t.shares if t.shares else 0.0),
            "shares": t.shares, "pnl": t.pnl, "r": t.r_multiple, "exit_reason": t.exit_reason,
            "holding_days": (t.exit_date - t.entry_date).days, "votes_for": "",
            "initial_stop": t.initial_stop, "atr": math.nan, "close": math.nan, "flags": "",
        })  # fmt: skip
    return pd.DataFrame(rows, columns=lg.COMMON)


def walk_forward(pm: pd.DataFrame, first_year: int = 2021) -> tuple[pd.DataFrame, list[str]]:
    """Every quarter: learn from trades finished BEFORE it, apply the shadow skips to its trades."""
    pm = lg.add_buckets(pm).sort_values("entry_date").reset_index(drop=True)
    quarters = pd.period_range(f"{first_year}-01", pm["entry_date"].max(), freq="Q")
    rows, log = [], []
    for q in quarters:
        start, end = q.start_time, q.end_time
        past = pm[pm["exit_date"] < start]
        now = pm[(pm["entry_date"] >= start) & (pm["entry_date"] <= end)]
        if now.empty:
            continue
        skip_rules = []
        if len(past) >= lg.MIN_TOTAL:
            res = lg.mine(past, DIMS)
            for x in res.lessons:
                ch = lg.skip_context(x.dim, x.value, f"c-{x.dim}-{x.value}")
                cr = lg.compare(past, ch)
                if cr.proposable and lg.triage(cr).action == "shadow_pause":
                    skip_rules.append((x.dim, str(x.value)))
        skipped_mask = pd.Series(False, index=now.index)
        for dim, val in skip_rules:
            skipped_mask |= now[dim].astype(str) == val
        kept, skipped = now[~skipped_mask], now[skipped_mask]
        rows.append({
            "quarter": str(q), "n": len(now), "n_skipped": len(skipped),
            "r_all": now["r"].sum(), "r_kept": kept["r"].sum(), "r_skipped": skipped["r"].sum(),
            "rules": "; ".join(f"{d}={v}" for d, v in skip_rules),
        })  # fmt: skip
        if skip_rules:
            log.append(f"- {q}: skip {', '.join(f'{d} = {v}' for d, v in skip_rules)} "
                       f"-> {len(skipped)} of {len(now)} trades skipped, they totalled "
                       f"{skipped['r'].sum():+.1f}R")  # fmt: skip
    return pd.DataFrame(rows), log


def part2(bars_dir: Path, start: date, end: date, hist: date) -> list[str]:
    members = universe_for("sp100")
    syms = sorted({BENCHMARK, *members})
    cache = BarCache(bars_dir)
    covered = [s for s in syms if cache.covers(s, hist, end)]
    bars = load_daily_bars(NoClient(), covered, hist, end, cache).bars
    sectors = {s: members.get(s, "") for s in bars}
    strat = get_strategy("trend_pullback")
    res = run_backtest(
        bars, sectors, strat, RiskConfig(gate_mode="win_rate"), BacktestConfig(start, end)
    )
    out = ["## 2. Real history: would learning have helped? (trend pullback, walk-forward)", ""]
    for label, trades in (("bot-taken", res.portfolio.trades), ("all-signals", res.signal_trades)):
        t = [
            x
            for x in trades
            if x.exit_reason != "end_of_data" and pd.Timestamp(start) <= x.entry_date
        ]
        common = to_common(t, sectors, label)
        pm = lg.build_postmortems(common, bars, bars.get(BENCHMARK))
        wf, log = walk_forward(pm)
        out.append(f"### {label}: {len(pm)} finished trades, {start} to {end}")
        out.append("")
        if wf.empty:
            out += ["Not enough history.", ""]
            continue
        used = wf[wf["n_skipped"] > 0]
        tot_all, tot_kept = wf["r_all"].sum(), wf["r_kept"].sum()
        skipped_r, skipped_n = wf["r_skipped"].sum(), int(wf["n_skipped"].sum())
        out += [
            f"- Quarters judged: {len(wf)}; quarters with any proposed skip: {len(used)}.",
            f"- Trades in those quarters: {int(wf['n'].sum())}; skipped by the loop: {skipped_n}.",
            f"- Total result (in times the amount risked, summed): all trades {tot_all:+.1f}R, "
            f"after the shadow skips {tot_kept:+.1f}R, the skipped trades alone {skipped_r:+.1f}R "
            f"(average {skipped_r / skipped_n:+.2f}R each)." if skipped_n else
            f"- Total result: {tot_all:+.1f}R; no skip survived the loop's tests.",
            "",
        ]  # fmt: skip
        full = lg.mine(lg.add_buckets(pm), DIMS)
        out.append(f"Whole-history look: {full.message}")
        for x in full.lessons[:5]:
            out.append(f"- Lesson: {lg.lesson_text(x)}")
        for x in full.watch[:3]:
            out.append(f"- Watch: {lg.lesson_text(x)}")
        if log:
            out += ["", "Quarter-by-quarter shadow skips:", *log[:12]]
        out.append("")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=Path, required=True, help="read-only bar cache directory")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2019, 1, 1))
    ap.add_argument("--history-start", type=date.fromisoformat, default=date(2018, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 9))
    a = ap.parse_args()
    lines = ["# Learning loop: does it work?", "", "Research only. No orders, no broker calls.", ""]
    lines += part1()
    lines += part2(a.bars, a.start, a.end, a.history_start)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
