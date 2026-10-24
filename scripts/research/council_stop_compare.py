# Author: Kai0421
"""Trailing stop vs fixed stop for the council paper ledger (research only: NO orders).

Two views, because the council itself has only a handful of buy verdicts so far:

1. the council's real buys from the shadow log (tiny n: shown for completeness);
2. the exit rule on its own: random stocks bought at the open, 3 a day for every session in
   the last year, with the same sizing/caps/exits as the ledger. This says whether trailing helps
   *as a stop rule*, independent of how good the council's picks are.

    uv run python scripts/research/council_stop_compare.py --log <council_shadow.csv> --env <.env>
"""

import argparse
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from bot import ricks_paper as rp
from bot.config import Settings, load_env_file
from bot.data import ET, load_daily_bars, make_data_client
from bot.universe import universe_for

SEED = 20261015
PER_DAY = 3


def stats(res: rp.PaperResult) -> dict:
    t = res.trades[res.trades["status"] == "closed"]
    eq = res.equity["equity"]
    dd = float(((eq.cummax() - eq) / eq.cummax()).max()) if len(eq) else 0.0
    ret = (t["pnl"] / (t["entry_price"] * t["shares"])) if len(t) else pd.Series(dtype=float)
    return {
        "trades": len(t),
        "win_rate": float((t["pnl"] > 0).mean()) if len(t) else float("nan"),
        "avg_return_pct": float(ret.mean() * 100) if len(t) else float("nan"),
        "sem_pct": float(ret.std(ddof=1) / np.sqrt(len(t)) * 100) if len(t) > 1 else float("nan"),
        "avg_R": float(t["r"].mean()) if len(t) else float("nan"),
        "total_pnl": float(t["pnl"].sum()) if len(t) else 0.0,
        "end_equity": float(eq.iloc[-1]) if len(eq) else rp.START_EQUITY,
        "max_drawdown_pct": dd * 100,
        "stop_exits": int((t["exit_reason"] == "stop").sum()) if len(t) else 0,
    }


def synthetic_log(bars: dict, sectors: dict, first: int) -> pd.DataFrame:
    rng = random.Random(SEED)
    syms = sorted(s for s in sectors if s in bars and s != "SPY")
    days = list(bars["SPY"].index)
    rows = []
    for d in days[first:-6]:
        for k, s in enumerate(rng.sample(syms, PER_DAY)):
            rows.append(
                {"day": d, "symbol": s, "crowd": 0.5, "tape": 0.6, "skeptic": 0.3,
                 "mean": 0.5 - 0.01 * k, "buy": True}
            )  # fmt: skip
    return pd.DataFrame(rows)


def show(title: str, log: pd.DataFrame, bars: dict, sectors: dict) -> None:
    fixed = stats(rp.replay(log, bars, sectors, trail=False))
    trail = stats(rp.replay(log, bars, sectors, trail=True))
    print(f"\n== {title}")
    print(pd.DataFrame({"fixed stop": fixed, "trailing stop": trail}).round(3).to_string())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--env", type=Path, required=True)
    args = ap.parse_args()
    now = datetime.now(ET)
    settings = Settings.from_env({**load_env_file(args.env), **os.environ})
    sectors = universe_for("sp100")
    council = pd.read_csv(args.log, parse_dates=["day"])
    bars = load_daily_bars(
        make_data_client(settings),
        sorted({*sectors, "SPY"}),
        (now - timedelta(days=420)).date(),
        now - timedelta(minutes=16),
        cache=None,
    ).bars
    today = pd.Timestamp(now.date())
    final = now.weekday() < 5 and (now.hour, now.minute) >= (16, 20)  # today's bar is complete
    done = today if final else today - pd.Timedelta(days=1)
    bars = {s: b.loc[:done] for s, b in bars.items()}
    show("council's real buys (tiny sample)", council, bars, sectors)
    show("exit rule alone: random stocks, 3 a day, last year", synthetic_log(bars, sectors, 20),
         bars, sectors)  # fmt: skip


if __name__ == "__main__":
    main()
