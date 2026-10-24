# Author: Kai0421
"""Council scorecard runner (research only: reads local files, NO orders, NO market calls).

Reads the paper ledger written by ``council_paper.py`` and the council shadow log, then shows
how the council is doing by week and overall: win rate with its 95% interval, average result,
profit vs SPY, rolling last-20, each Rick on his own, worst dip, stops raised, and the verdict
against the 100-trade bar. Also rewrites a self-contained HTML report.

    uv run python scripts/research/council_scorecard.py --print   # show it, no Slack
    uv run python scripts/research/council_scorecard.py --post    # weekly Slack post (once/week)

Not scheduled. The Ricks are fixed rules; "improving" means we change them on purpose and log
the change in docs/22-council-changelog.md, then compare each rules version separately.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bot import notify
from bot import ricks_scorecard as sc
from bot import ricks_shadow as rs


def read_csv(path: Path, dates: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, parse_dates=[c for c in dates if c in pd.read_csv(path, nrows=0)])


def load(args: argparse.Namespace) -> dict:
    trades = read_csv(args.data / "council_paper.csv", ["signal_day", "entry_date", "exit_date"])
    equity = read_csv(args.data / "council_paper_equity.csv", ["day"])
    raises = read_csv(args.data / "council_paper_stops.csv", ["day"])
    ablation = {}
    if args.log.exists():
        log = pd.read_csv(args.log, parse_dates=["day"])
        if "ret5" in log:
            ablation = rs.summary(rs.add_excess(log))
    return sc.build(trades, equity, raises, ablation)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="council_scorecard.py")
    ap.add_argument("--print", action="store_true", help="show the weekly message; no Slack")
    ap.add_argument("--post", action="store_true", help="send the weekly Slack post (once a week)")
    ap.add_argument("--data", type=Path, default=Path("data/research"))
    ap.add_argument("--log", type=Path, default=Path("data/research/council_shadow.csv"))
    ap.add_argument("--html", type=Path, default=Path("reports/research/council-scorecard.html"))
    args = ap.parse_args(argv)

    card = load(args)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(
        sc.html_report(card, f"Updated {datetime.now(UTC):%d %b %Y %H:%M} UTC"), "utf-8"
    )
    through = card["through"] if card["through"] is not None else pd.Timestamp.today()
    week = pd.Timestamp(through).to_period("W-SUN").start_time
    title, text = sc.weekly_message(card, week)
    print(f"{title}\n\n{text}\n\nHTML report: {args.html}")

    if args.post and not args.print:
        state_path = args.data / "council_scorecard_state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("posted_week") == str(week.date()):
            print("this week's scorecard was already posted")
            return 0
        try:
            rc = notify.main(["--category", "general", "--level", "INFO", "--title", title, text])
        except Exception as exc:
            print(f"slack post failed ({type(exc).__name__}); will retry")
            return 0
        if rc == 0:
            state["posted_week"] = str(week.date())
            state_path.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
