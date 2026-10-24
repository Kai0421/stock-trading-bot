# Author: Kai0421
"""Post a one-time note when the bot or the council reaches 30 finished trades (read-only).

    uv run python scripts/research/milestone_check.py --print   # show counts, post nothing
    uv run python scripts/research/milestone_check.py           # post what is due

Run from the lab folder. The bot's note goes to the normal channel, the council's to the council
channel. Counts: the bot's swing trades since --since (live journal, read-only, same rule as
docs/25) and the council's finished pretend trades. Safe to repeat: each is told once.
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from bot import milestones as ms
from bot import notify
from bot.success_test import describe, evaluate

LIVE_DIR = Path(__file__).resolve().parents[3] / "StockTradingBot"


def bot_counts(live: Path, since: str):
    try:
        verdict = evaluate(live / "data" / "bot.db", live / "data" / "bars" / "SPY.csv", since)
    except Exception as exc:  # unreadable journal: skip this week, never crash
        print(f"bot count unavailable ({type(exc).__name__})")
        return None, ""
    return verdict.trades, describe(verdict)


def council_counts(research: Path):
    path = research / "council_paper.csv"
    if not path.exists():
        return 0, "No finished pretend trades yet."
    closed = int((pd.read_csv(path, usecols=["status"])["status"] == "closed").sum())
    return closed, "See the weekly scorecard for how they did."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--live", type=Path, default=LIVE_DIR)
    ap.add_argument("--lab", type=Path, default=Path.cwd())
    ap.add_argument("--since", default="2026-10-13", help="first day of swing mode")
    args = ap.parse_args(argv)

    research = args.lab / "data" / "research"
    bot_n, bot_text = bot_counts(args.live, args.since)
    council_n, council_text = council_counts(research)
    counts = {"bot": bot_n, "council": council_n}
    texts = {"bot": bot_text, "council": council_text}
    print(f"finished trades: {counts}")
    state_path = research / "milestones.json"
    told = json.loads(state_path.read_text()) if state_path.exists() else {}
    for system in ms.due(counts, told):
        title, text = ms.message(system, counts[system], texts[system])
        print(f"due: {title}")
        if args.show:
            continue
        flags = ["--category", "general"]
        if notify.main([*flags, "--level", "INFO", "--title", title, text]) == 0:
            told[system] = ms.AT
            research.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(told))
    return 0


if __name__ == "__main__":
    sys.exit(main())
