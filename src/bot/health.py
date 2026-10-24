# Author: Kai0421
"""Morning health report: pure assessment and formatting (no I/O, no broker writes).

``scripts/health_report.py`` gathers the inputs (Alpaca reads, the Windows task list, the
journal, the collector and shadow-log files) and posts the result to Slack. Any doubt is a
problem: a position with no stop is CRITICAL, a stale or failing task is a warning.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from bot.slack_format import bullets, section, table

NEVER_RAN = {267011, 267009, 267008}  # Task Scheduler: not yet run / running / ready
TICK_WARN_MIN = 15  # the bot ticks every 5 minutes
TICK_CRITICAL_MIN = 60
OTHER_STALE_MIN = 180  # the 30-minute helper tasks
OUTBOX_STALE_MIN = 10  # queued messages should leave within a minute or two
DAILY_TASKS = {"health-report"}  # run once a weekday, so its age is not a signal
# The bot report watches the bot's own tasks only. The council's jobs are checked in its own
# report (scripts/council_health.py); weekly and weekday-only jobs would look stale here.
BOT_TASKS = frozenset({"tick", "overnight-shadow", "health-report", "notifier-drain"})


@dataclass(frozen=True)
class TaskInfo:
    name: str
    state: str
    last_run: datetime | None
    result: int | None


@dataclass(frozen=True)
class PositionLine:
    symbol: str
    qty: float
    avg_entry: float
    last: float | None
    pnl: float | None
    pnl_pct: float | None
    has_stop: bool
    exiting: bool = False  # a sell order is queued for the open (the stop was swapped for it)


@dataclass(frozen=True)
class Issue:
    level: str  # "WARN" | "CRITICAL"
    text: str


@dataclass(frozen=True)
class HealthInputs:
    now: datetime
    tasks: Sequence[TaskInfo]
    broker_error: str | None
    account_status: str
    account_blocked: bool
    equity: float | None
    cash: float | None
    positions: Sequence[PositionLine]
    open_orders: Sequence[str]
    flags: dict[str, str]
    last_runs: dict[str, str]
    recent_trades: Sequence[tuple[str, str, float, str]]  # symbol, exit date, pnl, reason
    market_open: bool | None
    next_open: str
    on_battery: bool | None
    uptime_hours: float | None
    extras: dict[str, str] = field(default_factory=dict)  # collector / shadow-log lines
    outbox_waiting: int = 0  # Slack messages queued and not yet sent (NOTIFY_VIA_OUTBOX)
    outbox_oldest_min: float | None = None  # minutes the oldest one has waited
    outbox_failed: int = 0  # given up on after repeated failures


def assess(h: HealthInputs) -> list[Issue]:
    issues: list[Issue] = []
    if h.broker_error:
        issues.append(Issue("CRITICAL", f"Alpaca not reachable: {h.broker_error}"))
    if h.account_blocked or (h.account_status and h.account_status != "active"):
        issues.append(Issue("CRITICAL", f"account not active ({h.account_status})"))
    for p in h.positions:
        if not p.has_stop and not p.exiting:
            issues.append(Issue("CRITICAL", f"{p.symbol} has no broker-side stop order"))
    for t in (t for t in h.tasks if t.name in BOT_TASKS):
        age = (h.now - t.last_run).total_seconds() / 60 if t.last_run else None
        if t.result not in (None, 0) and t.result not in NEVER_RAN:
            issues.append(Issue("WARN", f"task {t.name} last result {t.result}"))
        if t.name == "tick" and age is not None:
            if age > TICK_CRITICAL_MIN:
                issues.append(Issue("CRITICAL", f"tick has not run for {age:.0f} min"))
            elif age > TICK_WARN_MIN:
                issues.append(Issue("WARN", f"tick last ran {age:.0f} min ago"))
        elif t.name not in DAILY_TASKS and age is not None and age > OTHER_STALE_MIN:
            issues.append(Issue("WARN", f"task {t.name} last ran {age / 60:.1f} h ago"))
    for name, reason in h.flags.items():
        issues.append(Issue("WARN", f"flag set: {name} ({reason})"))
    for job, status in h.last_runs.items():
        if status not in ("ok", "running"):
            issues.append(Issue("WARN", f"job {job} last status: {status}"))
    if h.outbox_oldest_min is not None and h.outbox_oldest_min > OUTBOX_STALE_MIN:
        issues.append(
            Issue(
                "WARN",
                f"{h.outbox_waiting} Slack message(s) are waiting to be sent, the oldest for "
                f"{h.outbox_oldest_min:.0f} min: the notifier-drain task may have stopped",
            )
        )
    if h.outbox_failed:
        issues.append(Issue("WARN", f"{h.outbox_failed} Slack message(s) could not be sent"))
    if h.on_battery:
        issues.append(Issue("WARN", "laptop is on battery (it sleeps in 3 min); plug it in"))
    return issues


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.0f}"


def _signed(x: float) -> str:
    return f"{x:+,.2f}"


def render(h: HealthInputs, issues: Sequence[Issue]) -> tuple[str, str, str]:
    """Returns (level, title, text) for the Slack message: bullets for lists, tables for rows."""
    level = (
        "CRITICAL" if any(i.level == "CRITICAL" for i in issues) else "WARN" if issues else "INFO"
    )
    title = "Morning health: OK" if not issues else "Morning health: ATTENTION"
    blocks: list[str] = []
    if issues:
        blocks.append(
            bullets([f"{'🔴' if i.level == 'CRITICAL' else '🟡'} {i.text}" for i in issues])
        )
    market = "open" if h.market_open else f"closed, next open {h.next_open}"
    blocks.append(
        section(
            "Account",
            bullets(
                [
                    f"Status: {h.account_status or 'unknown'}",
                    f"Equity: {_usd(h.equity)}",
                    f"Cash: {_usd(h.cash)}",
                    f"Market: {market}",
                ]
            ),
        )
    )
    if h.positions:
        rows = []
        for p in h.positions:
            pl = "n/a" if p.pnl is None else f"{_signed(p.pnl)} ({p.pnl_pct:+.1%})"
            stop = "stop ✓" if p.has_stop else "selling at the open" if p.exiting else "NO STOP"
            rows.append([p.symbol, f"{p.qty:g}", f"{p.avg_entry:,.2f}", pl, stop])
        blocks.append(
            section(
                f"Open positions ({len(h.positions)})",
                table(["Stock", "Shares", "Bought at", "Profit/loss", "Protection"], rows),
            )
        )
    else:
        blocks.append("No open positions.")
    if h.open_orders:
        blocks.append(section("Open orders", bullets(h.open_orders)))
    if h.recent_trades:
        rows = [[s, d, _signed(pnl), why.replace("_", " ")] for s, d, pnl, why in h.recent_trades]
        blocks.append(
            section("Last closed trades", table(["Stock", "Sold on", "Profit/loss", "Why"], rows))
        )
    tasks = [t for t in h.tasks if t.name in BOT_TASKS]
    blocks.append(
        section(
            "Scheduled tasks",
            bullets(
                [
                    f"{t.name} {'✓' if t.result in (0, None) or t.result in NEVER_RAN else '✗'}"
                    for t in tasks
                ]
            ),
        )
    )
    if h.extras:
        blocks.append(bullets([f"{k}: {v}" for k, v in h.extras.items()]))
    if h.uptime_hours is not None:
        blocks.append(f"Laptop up {h.uptime_hours:.0f} h.")
    return level, title, "\n\n".join(b for b in blocks if b)
