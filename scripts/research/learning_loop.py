# Author: Kai0421
"""Learning loop runner (research only: reads files, writes local reports, NO orders).

Looks back at every finished trade (the live bot's journal, read-only, and the council practice
account), writes a plain-English post-mortem for each, looks for situations where losses
cluster, tests "what if we had skipped it" challengers on later trades only, and writes proposals.
It never changes the live bot: tightening ideas are shadow recommendations, anything that loosens
or adds risk is a proposal for the owner's reviewed commit (docs/23-learning-loop.md).

    uv run python scripts/research/learning_loop.py --print       # write files, no Slack
    uv run python scripts/research/learning_loop.py --post        # also post the weekly digest

Not scheduled. Safe to repeat: everything is recomputed from the journal, the ledger and the bars.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from bot import learning as lg
from bot import notify
from bot.data import ET, BarCache
from bot.universe import universe_for


def read_journal(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The bot's journal opened read-only (it cannot be written from here)."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        trades = pd.read_sql_query("SELECT * FROM trades ORDER BY id", con)
        signals = pd.read_sql_query("SELECT * FROM signals", con)
    finally:
        con.close()
    return trades, signals


def read_bars(cache: BarCache, symbols: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        df = cache.read(s)  # read-only: never fetches, never writes
        if df is not None and len(df):
            out[s] = df
    return out


def run(args: argparse.Namespace) -> int:
    sectors = universe_for("sp100")
    live = pd.DataFrame(columns=lg.COMMON)
    if args.journal.exists():
        trades, signals = read_journal(args.journal)
        live = lg.normalise_live(trades, signals, sectors)
    council = pd.DataFrame(columns=lg.COMMON)
    if args.council.exists():
        council = lg.normalise_council(pd.read_csv(args.council))
    allt = pd.concat([live, council], ignore_index=True)
    bars = read_bars(BarCache(args.bars), sorted({*allt["symbol"], "SPY"})) if len(allt) else {}
    pm = (
        lg.build_postmortems(allt, bars, bars.get("SPY"))
        if len(allt)
        else pd.DataFrame(columns=lg.COLUMNS)
    )
    rep = lg.build_report(pm, live_trades=len(live), council_trades=len(council))
    title, text = lg.digest(rep)

    args.out.mkdir(parents=True, exist_ok=True)
    pm.to_csv(args.out / "postmortems.csv", index=False)
    lines = [f"# {title}", "", f"Generated {datetime.now(ET):%Y-%m-%d %H:%M}.", "", text, ""]
    if len(pm):
        lines += ["## Every finished trade, in plain English", ""]
        lines += [f"- {x}" for x in pm.sort_values("exit_date")["lesson"]]
    (args.out / "learning_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    props = [t.message for t in rep.triage if t.action != "none"]
    (args.out / "learning_proposals.md").write_text(
        "# Proposals (nothing applied to the live bot)\n\n"
        + ("\n".join(f"- {p}" for p in props) if props else "None this week.\n"),
        encoding="utf-8",
    )
    print(text)
    print()
    print(f"{len(pm)} post-mortems written to {args.out / 'postmortems.csv'}")
    for line in pm.sort_values("exit_date")["lesson"] if len(pm) else []:
        print(f" - {line}")

    if args.post:
        state_path = args.out / "learning_state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        week = f"{datetime.now(ET):%G-W%V}"
        if state.get("posted_week") == week:
            print(f"already posted for {week}")
            return 0
        rc = notify.main(["--category", "general", "--level", "INFO", "--title", title, text])
        if rc == 0:
            state["posted_week"] = week
            state_path.write_text(json.dumps(state))
        return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="learning_loop.py")
    ap.add_argument("--journal", type=Path, default=Path("data/bot.db"), help="read-only")
    ap.add_argument("--council", type=Path, default=Path("data/research/council_paper.csv"))
    ap.add_argument("--bars", type=Path, default=Path("data/bars"), help="read-only bar cache")
    ap.add_argument("--out", type=Path, default=Path("data/research"))
    ap.add_argument("--print", action="store_true", help="write files only, no Slack (default)")
    ap.add_argument("--post", action="store_true", help="post the weekly digest to Slack")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
