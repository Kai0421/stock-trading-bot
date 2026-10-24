# Author: Kai0421
"""Shadow test (read-only): what would a sell-at-+1.5R target have done on the bot's real trades?

For every trade in the live journal (finished or still open) this replays the daily bars and asks
whether the day's high reached entry + 1.5 x risk before the bot actually sold. If so the shadow
trade is sold at that target (a day that also touches the stop counts as the stop). It changes
nothing and places no orders; the live exit is untouched. See docs/26 (ideas 30, 31): in the
backtest this target was not significantly different from the live exit, so this checks it on the
bot's own future trades.

    uv run python scripts/research/exit_shadow.py --live ../StockTradingBot --env .env
    uv run python scripts/research/exit_shadow.py --print    # show the table, write nothing

Writes reports/research/exit-shadow.csv and exit-shadow.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bot import luck_test as lt
from bot.config import Settings, load_env_file
from bot.data import load_daily_bars, make_data_client
from bot.learning import risk_per_share

TARGET_R = 1.5
TELL_AT = 30


def verdict(done: pd.DataFrame) -> tuple[str, str]:
    """Plain-English one-time note for when enough trades have finished."""
    diff = done["shadow_r"] - done["actual_r"]
    res = lt.mean_test(diff)
    half = len(diff) // 2
    both = diff.iloc[:half].mean() > 0 and diff.iloc[half:].mean() > 0
    good = res.low > 0 and both
    answer = (
        "Yes, it looks good: selling at +1.5R beat the current exit by a clear margin, "
        "in both halves."
        if good
        else "No, not good enough to change anything: the gain is not clearly above zero, so the "
        "current exit stays."
    )
    title = f"Sell-at-+1.5R test: {len(diff)} finished trades"
    text = (
        f"You asked to be told. {answer}"
        + chr(10)
        + f"- Average difference per trade: {res.mean:+.3f}R "
        f"(95% range {res.low:+.3f}R to {res.high:+.3f}R)."
        + chr(10)
        + "- Nothing in the live bot was changed; changing the exit needs your OK."
    )
    return title, text


def journal_trades(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) | {"status": "closed"} for r in con.execute("SELECT * FROM trades")]
    for r in con.execute("SELECT * FROM positions"):
        p = dict(r)
        rows.append(
            {
                "symbol": p["symbol"],
                "entry_date": p["entry_date"],
                "exit_date": None,
                "entry_price": p["entry_price"],
                "initial_stop": p["initial_stop"],
                "exit_price": None,
                "r_multiple": None,
                "status": "open",
            }
        )
    con.close()
    return rows


def replay(t: dict, bars: pd.DataFrame) -> dict:
    entry, risk = float(t["entry_price"]), risk_per_share(t | {"r": t.get("r_multiple")})
    start = pd.Timestamp(t["entry_date"])
    end = pd.Timestamp(t["exit_date"]) if t["exit_date"] else None
    stop = entry - risk
    window = bars[bars.index >= start]
    if end is not None:
        window = window[window.index <= end]
    hit, best = None, 0.0
    for day, b in window.iterrows():
        if b["low"] <= stop:
            break  # the stop came first: shadow and live agree
        best = max(best, (b["high"] - entry) / risk)
        if b["high"] >= entry + TARGET_R * risk:
            hit = day
            break
    actual = t["r_multiple"]
    if actual is None and len(window):  # still open: mark to the latest close
        actual = (float(window["close"].iloc[-1]) - entry) / risk
    shadow = TARGET_R if hit is not None else actual
    return {
        "symbol": t["symbol"],
        "entry_date": str(start.date()),
        "status": t["status"],
        "best_r_since_entry": round(best, 2),
        "target_hit_on": str(hit.date()) if hit is not None else "",
        "actual_r": None if actual is None else round(float(actual), 3),
        "shadow_r": None if shadow is None else round(float(shadow), 3),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=Path, default=Path("../StockTradingBot"))
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--out-dir", type=Path, default=Path("reports/research"))
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--notify", action="store_true", help="tell the owner once at 30 trades")
    args = ap.parse_args(argv)

    trades = journal_trades(args.live / "data" / "bot.db")
    if not trades:
        print("no trades yet")
        return 0
    settings = Settings.from_env({**load_env_file(args.env), **os.environ})
    syms = sorted({t["symbol"] for t in trades})
    first = min(pd.Timestamp(t["entry_date"]) for t in trades).date()
    loaded = load_daily_bars(
        make_data_client(settings), syms, first, datetime.now(UTC), None, feed="iex"
    )
    rows = [replay(t, loaded.bars[t["symbol"]]) for t in trades if t["symbol"] in loaded.bars]
    df = pd.DataFrame(rows).sort_values("entry_date")
    done = df[(df["status"] == "closed") & df["shadow_r"].notna()]
    lines = [
        "# Shadow test: sell at +1.5R vs the live exit (bot's own trades)",
        "",
        "| Stock | Bought | Status | Best it reached (R) | Target hit on | Live result (R) | "
        "Shadow result (R) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in df.itertuples():
        lines.append(
            f"| {r.symbol} | {r.entry_date} | {r.status} | {r.best_r_since_entry:+.2f} | "
            f"{r.target_hit_on or '-'} | {r.actual_r} | {r.shadow_r} |"
        )
    lines.append("")
    if len(done) >= 2:
        diff = done["shadow_r"] - done["actual_r"]
        res = lt.mean_test(diff)
        lines.append(
            f"Finished trades: {len(done)}. Shadow minus live, average per trade: "
            f"{res.mean:+.3f}R (95% range {res.low:+.3f}R to {res.high:+.3f}R). "
            "Nothing to conclude before about 30 finished trades."
        )
    else:
        lines.append("Fewer than 2 finished trades so far: too early to compare.")
    text = "\n".join(lines) + "\n"
    print(text)
    if not args.show:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.out_dir / "exit-shadow.csv", index=False)
        (args.out_dir / "exit-shadow.md").write_text(text, encoding="utf-8")
    state = args.out_dir / "exit-shadow-told.json"
    if args.notify and not args.show and len(done) >= TELL_AT and not state.exists():
        from bot import notify

        title, msg = verdict(done)
        if notify.main(["--category", "general", "--level", "INFO", "--title", title, msg]) == 0:
            state.write_text(json.dumps({"told_at": len(done)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
