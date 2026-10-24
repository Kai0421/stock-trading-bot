# Author: Kai0421
"""One-off research (cached daily bars, no orders): does the Stocktwits sentiment score, alone or
with the price trend, predict the next days' return? Run from the repo root:

    uv run python scripts/research/crowd_history_backtest.py [bars_dir]

Input: reports/research/stocktwits-history-pilot.json (daily 0-100 scores from the Stocktwits
connector, 6 months, a pilot set of liquid stocks). The score at the close of day t is the
signal; the trade enters at the OPEN of day t+1 (so it uses nothing newer than the signal)
and exits at the close of day t+h. Excess return = stock return minus the average return of
all pilot stocks over the same dates, so a rising market does not look like skill.

Limits: ~125 days, ~11 stocks, heavily overlapping signals (a score stays high for days), the
connector's score may be re-normalised after the fact (look-ahead not ruled out), and only the
score is available historically (no message volume or tagged-post net). Treat as a smoke test.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from bot.data import BarCache
from bot.ricks import tape_inputs, tape_vote

HIST = Path("reports/research/stocktwits-history-pilot.json")
HORIZONS = (1, 5)
BUCKETS = [(0, 30), (30, 45), (45, 60), (60, 70), (70, 101)]


def build(bars_dir: str) -> pd.DataFrame:
    data = json.loads(HIST.read_text())
    dates = pd.to_datetime(data["dates"])
    cache = BarCache(bars_dir)
    rows = []
    for sym, scores in data["scores"].items():
        bars = cache.read(sym)
        if bars is None:
            continue
        s = pd.Series(scores, index=dates)
        for i, day in enumerate(dates):
            if day not in bars.index:
                continue
            pos = bars.index.get_loc(day)
            rec = {"symbol": sym, "day": day, "score": s.iloc[i]}
            rec["change3"] = s.iloc[i] - s.iloc[i - 3] if i >= 3 else np.nan
            entry = bars["open"].iloc[pos + 1] if pos + 1 < len(bars) else np.nan
            for h in HORIZONS:
                exit_ = bars["close"].iloc[pos + h] if pos + h < len(bars) else np.nan
                rec[f"ret{h}"] = exit_ / entry - 1
            rec["tape"] = tape_vote(tape_inputs(bars.iloc[: pos + 1])).degree
            rows.append(rec)
    df = pd.DataFrame(rows)
    for h in HORIZONS:  # market-adjust against the average of the pilot stocks on that day
        df[f"x{h}"] = df[f"ret{h}"] - df.groupby("day")[f"ret{h}"].transform("mean")
    df["crowd"] = (df["score"] - 50) / 50
    return df.dropna(subset=["x1", "x5"])


def summarize(name: str, sel: pd.DataFrame) -> dict:
    out = {"rule": name, "n": len(sel)}
    for h in HORIZONS:
        x = sel[f"x{h}"]
        daily = sel.groupby("day")[f"x{h}"].mean()  # one value per day
        t = daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily))) if len(daily) > 2 else np.nan
        out[f"mean_x{h}_bps"] = round(float(x.mean()) * 1e4, 1)
        out[f"hit{h}"] = round(float((sel[f"ret{h}"] > 0).mean()), 3)
        out[f"t{h}"] = round(float(t), 2) if t == t else np.nan
    out["days"] = sel["day"].nunique()
    return out


def main() -> None:
    bars_dir = sys.argv[1] if len(sys.argv) > 1 else "data/bars"
    df = build(bars_dir)
    print(f"{df.symbol.nunique()} stocks, {df.day.nunique()} days, {len(df)} stock-days")
    rules = [("all stock-days (baseline)", df)]
    for lo, hi in BUCKETS:
        rules.append((f"score {lo}-{hi - 1}", df[(df.score >= lo) & (df.score < hi)]))
    rules += [
        ("score >= 70 and up >= 10 in 3d", df[(df.score >= 70) & (df.change3 >= 10)]),
        ("score <= 30 and down >= 10 in 3d", df[(df.score <= 30) & (df.change3 <= -10)]),
        ("crowd for (score >= 65)", df[df.crowd >= 0.3]),
        ("tape for (>= 0.3)", df[df.tape >= 0.3]),
        ("crowd + tape both for", df[(df.crowd >= 0.3) & (df.tape >= 0.3)]),
        ("crowd for, tape against", df[(df.crowd >= 0.3) & (df.tape <= -0.3)]),
        ("crowd against (score <= 35)", df[df.crowd <= -0.3]),
        ("crowd against, tape for", df[(df.crowd <= -0.3) & (df.tape >= 0.3)]),
    ]
    table = pd.DataFrame([summarize(n, s) for n, s in rules if len(s) > 5])
    pd.set_option("display.width", 200)
    print(table.to_string(index=False))
    out = Path("reports/research/crowd-history-backtest-2026-10-11.csv")
    table.to_csv(out, index=False)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
