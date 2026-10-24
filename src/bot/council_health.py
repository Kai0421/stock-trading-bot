# Author: Kai0421
"""Council of Ricks health: pure checks over what the helper jobs left behind.

Tells the owner, in plain English, whether the council's own jobs are running and its data is
fresh. Read-only and never CRITICAL: nothing here trades. The trading bot has its own report.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bot.data import ET
from bot.health import TaskInfo
from bot.ricks_paper import GATE_TRADES

TASK_MAX_AGE_H = {"council-shadow": 3.0, "sentiment-collect": 3.0, "council-paper": 30.0}
RUNNING, NEVER_RUN = 267009, 267011  # Windows task codes that are not failures


@dataclass(frozen=True)
class CouncilInputs:
    now: datetime  # timezone-aware
    tasks: Sequence[TaskInfo]
    shadow_last_day: date | None
    shadow_days: int
    collector_last_full_day: date | None  # the Stocktwits collector runs about once a day
    ledger_last_day: date | None
    summary_day: date | None
    rules_version: str
    open_trades: int
    closed_trades: int


def expected_session(now: datetime) -> date:
    """The latest US session whose council day should already be recorded (ET, weekdays only)."""
    et = now.astimezone(ET)
    day = et.date() if et.weekday() < 5 and et.hour >= 17 else et.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _hours(n: float) -> str:
    return f"{n:.0f} hour{'s' if round(n) != 1 else ''}"


def _task_issues(h: CouncilInputs) -> list[str]:
    by_name = {t.name: t for t in h.tasks}
    out = []
    for name, max_age in TASK_MAX_AGE_H.items():
        t = by_name.get(name)
        if t is None:
            out.append(f"The {name} task was not found on this laptop.")
            continue
        if t.state == "Disabled":
            out.append(f"The {name} task is switched off.")
        if t.result not in (None, 0, RUNNING, NEVER_RUN):
            out.append(f"The {name} task's last run failed.")
        if t.last_run is not None:
            age = (h.now - t.last_run).total_seconds() / 3600
            if age > max_age:
                out.append(f"The {name} task has not run for {_hours(age)}.")
    return out


def assess(h: CouncilInputs) -> list[str]:
    issues = _task_issues(h)
    want = expected_session(h.now)
    if h.collector_last_full_day is None:
        issues.append("The Stocktwits collector has not recorded a full day yet.")
    elif h.collector_last_full_day < want:
        issues.append(
            f"The Stocktwits data was last fully collected for {h.collector_last_full_day:%d %b}; "
            f"{want:%d %b} should be there."
        )
    if h.shadow_last_day is None:
        issues.append("No council days have been recorded yet.")
    elif h.shadow_last_day < want:
        issues.append(
            f"The last recorded council day is {h.shadow_last_day:%d %b}; "
            f"{want:%d %b} should be there."
        )
    if h.ledger_last_day is not None and (
        h.summary_day is None or h.summary_day < h.ledger_last_day
    ):
        issues.append(
            f"The practice account's daily summary for {h.ledger_last_day:%d %b} has not been "
            "sent yet."
        )
    return issues


def render(h: CouncilInputs) -> tuple[str, str, str]:
    """(level, title, text) for Slack."""
    issues = assess(h)
    lines = [f"🟡 {i}" for i in issues] if issues else ["Everything is running as it should."]
    lines.append("")
    if h.shadow_last_day is None:
        lines.append("Council log: no council days recorded yet.")
    else:
        lines.append(
            f"Council log: {h.shadow_days} days recorded, latest {h.shadow_last_day:%d %b}."
        )
    lines.append(
        f"Practice account: {h.open_trades} pretend trade{'s' if h.open_trades != 1 else ''} open, "
        f"{h.closed_trades} of {GATE_TRADES} finished. Rules version {h.rules_version}."
    )
    lines.append("Practice only: no real order was placed and no real money is involved.")
    return (
        "WARN" if issues else "INFO",
        "Council health: ATTENTION" if issues else "Council health: OK",
        "\n".join(lines),
    )
