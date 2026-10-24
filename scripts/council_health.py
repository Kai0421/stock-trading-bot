# Author: Kai0421
"""Council of Ricks health report to Slack (reads only: the Windows task list and local files).

    uv run python scripts/council_health.py            # post to the council channel
    uv run python scripts/council_health.py --print    # show it, do not post

Run it from the lab folder (the council's data and Slack settings live there). Checks the council's
own helper jobs and data (Stocktwits collector, council shadow log, practice ledger and its daily
summary). Never touches the trading bot, a broker or an order. The bot has its own report
(scripts/health_report.py).
"""

import argparse
import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from bot import council_health as ch
from bot import notify
from bot.ricks_paper import RULES_VERSION

_spec = importlib.util.spec_from_file_location(
    "health_report", Path(__file__).with_name("health_report.py")
)
hr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hr)  # reuse its Windows task reader


def _day(value) -> date | None:
    try:
        return pd.Timestamp(value).date()
    except (ValueError, TypeError):
        return None


def read_inputs(lab: Path, now: datetime) -> ch.CouncilInputs:
    research = lab / "data" / "research"
    shadow_last, shadow_days = None, 0
    if (research / "council_shadow.csv").exists():
        days = pd.read_csv(research / "council_shadow.csv", usecols=["day"])["day"]
        shadow_days = int(days.nunique())
        shadow_last = _day(days.max()) if len(days) else None
    ledger_last = None
    if (research / "council_paper_equity.csv").exists():
        eq = pd.read_csv(research / "council_paper_equity.csv", usecols=["day"])
        ledger_last = _day(eq["day"].max()) if len(eq) else None
    open_trades = closed_trades = 0
    if (research / "council_paper.csv").exists():
        status = pd.read_csv(research / "council_paper.csv", usecols=["status"])["status"]
        open_trades, closed_trades = int((status == "open").sum()), int((status == "closed").sum())
    summary_day = None
    state = research / "council_paper_state.json"
    if state.exists():
        try:
            summary_day = _day(json.loads(state.read_text()).get("summary_day"))
        except (json.JSONDecodeError, OSError):
            summary_day = None
    collector_day = None
    cstate = lab / "data" / "sentiment" / "state.json"
    if cstate.exists():
        try:
            collector_day = _day(json.loads(cstate.read_text()).get("last_full_day"))
        except (json.JSONDecodeError, OSError):
            collector_day = None
    return ch.CouncilInputs(
        now=now,
        tasks=hr.read_tasks(),
        shadow_last_day=shadow_last,
        shadow_days=shadow_days,
        collector_last_full_day=collector_day,
        ledger_last_day=ledger_last,
        summary_day=summary_day,
        rules_version=RULES_VERSION,
        open_trades=open_trades,
        closed_trades=closed_trades,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true", help="print only, no Slack")
    ap.add_argument("--lab", type=Path, default=Path.cwd())
    args = ap.parse_args(argv)

    level, title, text = ch.render(read_inputs(args.lab, datetime.now(UTC)))
    print(f"[{level}] {title}\n{text}")
    if args.show:
        return 0
    return notify.main(["--category", "general", "--level", level, "--title", title, text])


if __name__ == "__main__":
    sys.exit(main())
