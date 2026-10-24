# Author: Kai0421
"""Morning health report to Slack (reads only: Alpaca, the Windows task list, local files).

    uv run python scripts/health_report.py            # post to Slack
    uv run python scripts/health_report.py --print    # show it, do not post

Checks the live bot only (its scheduled tick, its journal, the Alpaca account, open positions
and their stops). The Council of Ricks has its own report (scripts/council_health.py). Never
places, changes or cancels an order. Scheduled weekdays by install_windows_task.ps1 -HealthReport.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bot import health as hl
from bot import notify
from bot.broker import Broker
from bot.config import Settings, load_env_file

LIVE_DIR = Path(__file__).resolve().parents[2] / "StockTradingBot"  # sibling live folder
LAB_DIR = Path(__file__).resolve().parents[1]
ET_NAME = "America/New_York"

TASKS_PS = (
    "Get-ScheduledTask -TaskPath '\\StockTradingBot\\' | ForEach-Object { "
    "$i = Get-ScheduledTaskInfo $_; [pscustomobject]@{ Name = $_.TaskName; State = [string]$_.State; "  # noqa: E501
    "Last = $(if ($i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToUniversalTime().ToString('o') } "
    "else { $null }); Result = $i.LastTaskResult } } | ConvertTo-Json -Compress"
)
MACHINE_PS = (
    "$b = Get-CimInstance Win32_Battery; $os = Get-CimInstance Win32_OperatingSystem; "
    "[pscustomobject]@{ OnBattery = $(if ($b) { $b.BatteryStatus -eq 1 } else { $false }); "
    "UptimeH = ((Get-Date) - $os.LastBootUpTime).TotalHours } | ConvertTo-Json -Compress"
)


def powershell_json(script: str):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60
    ).stdout.strip()
    if not out:
        return None
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def read_tasks() -> list[hl.TaskInfo]:
    tasks = []
    for t in powershell_json(TASKS_PS) or []:
        last = datetime.fromisoformat(t["Last"]).astimezone(UTC) if t.get("Last") else None
        tasks.append(hl.TaskInfo(t["Name"], t["State"], last, t["Result"]))
    return sorted(tasks, key=lambda t: t.name)


def read_journal(live: Path) -> tuple[dict[str, str], dict[str, str], list]:
    """last job statuses, set flags, last 5 closed trades. Read-only; empty on any problem."""
    db = live / "data" / "bot.db"
    if not db.exists():
        return {}, {}, []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        runs = {
            r["job"]: r["status"]
            for r in con.execute(
                "SELECT job, status FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY job)"
            )
        }
        flags = {
            r["name"]: r["reason"] or ""
            for r in con.execute("SELECT name, reason FROM flags WHERE value = 1")
        }
        trades = [
            (r["symbol"], r["exit_date"], r["pnl"], r["exit_reason"])
            for r in con.execute(
                "SELECT symbol, exit_date, pnl, exit_reason FROM trades ORDER BY id DESC LIMIT 5"
            )
        ]
        con.close()
        return runs, flags, trades
    except sqlite3.Error:
        return {}, {}, []


def read_outbox(live: Path, now: datetime) -> tuple[int, float | None, int]:
    """Messages waiting in the notification queue, the age of the oldest in minutes, and how many
    were given up on. Read-only; (0, None, 0) when the queue is off or unreadable."""
    db = live / "data" / "notifier_outbox.db"
    if not db.exists():
        return 0, None, 0
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        waiting, oldest = con.execute(
            "SELECT COUNT(*), MIN(created) FROM messages WHERE status = 'queued'"
        ).fetchone()
        failed = con.execute("SELECT COUNT(*) FROM messages WHERE status = 'failed'").fetchone()[0]
        con.close()
    except sqlite3.Error:
        return 0, None, 0
    age = None
    if oldest:
        made = datetime.fromisoformat(oldest)
        made = made if made.tzinfo else made.replace(tzinfo=ZoneInfo(ET_NAME))
        age = (now - made).total_seconds() / 60
    return int(waiting), age, int(failed)


def read_broker(settings: Settings):
    """account, positions with stops, open order text, clock. Any failure -> an error string."""
    try:
        b = Broker.from_settings(settings)
        acct, clock = b.account(), b.clock()
        orders = b.open_orders()
        stops: set[str] = set()
        exits: set[str] = set()
        flat: list = []

        def walk(o):
            flat.append(o)
            for leg in o.legs:
                walk(leg)

        for o in orders:
            walk(o)
        for o in flat:
            if o.side == "sell" and o.type in ("stop", "stop_limit", "trailing_stop"):
                stops.add(o.symbol)
            elif o.side == "sell" and o.type == "market":
                exits.add(o.symbol)  # the open sell that replaces the stop before 09:28 ET
        positions = []
        for p in b.positions().values():
            pnl = (p.current_price - p.avg_entry_price) * p.qty if p.current_price else None
            pct = p.current_price / p.avg_entry_price - 1 if p.current_price else None
            positions.append(
                hl.PositionLine(
                    p.symbol,
                    p.qty,
                    p.avg_entry_price,
                    p.current_price or None,
                    pnl,
                    pct,
                    p.symbol in stops,
                    p.symbol in exits and p.symbol not in stops,
                )
            )
        text = [
            f"{o.side} {o.type} {o.symbol} {o.qty:g}"
            + (f" @ {o.stop_price:g}" if o.stop_price else "")
            for o in orders
        ]
        return acct, clock, positions, text, None
    except Exception as exc:  # report it; the health report must never crash
        return None, None, [], [], f"{type(exc).__name__}: {exc}"[:200]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true", help="print only, no Slack")
    ap.add_argument("--live", type=Path, default=LIVE_DIR)
    args = ap.parse_args(argv)

    now = datetime.now(UTC)
    settings = Settings.from_env({**load_env_file(Path(".env")), **os.environ})
    acct, clock, positions, orders, error = read_broker(settings)
    runs, flags, trades = read_journal(args.live)
    waiting, oldest, failed = read_outbox(args.live, now)
    machine = (powershell_json(MACHINE_PS) or [{}])[0]
    inputs = hl.HealthInputs(
        now=now,
        tasks=read_tasks(),
        broker_error=error,
        account_status=acct.status if acct else "",
        account_blocked=bool(acct and acct.blocked),
        equity=acct.account.equity if acct else None,
        cash=acct.account.cash if acct else None,
        positions=positions,
        open_orders=orders,
        flags=flags,
        last_runs=runs,
        recent_trades=trades,
        market_open=clock.is_open if clock else None,
        next_open=(
            clock.next_open.astimezone(__import__("zoneinfo").ZoneInfo(ET_NAME)).strftime(
                "%a %H:%M ET"
            )
            if clock
            else "unknown"
        ),
        on_battery=machine.get("OnBattery"),
        uptime_hours=machine.get("UptimeH"),
        outbox_waiting=waiting,
        outbox_oldest_min=oldest,
        outbox_failed=failed,
        extras={},  # council and collector checks live in scripts/council_health.py
    )
    level, title, text = hl.render(inputs, hl.assess(inputs))
    print(f"[{level}] {title}\n{text}")
    if args.show:
        return 0
    return notify.main(["--category", "general", "--level", level, "--title", title, text])


if __name__ == "__main__":
    sys.exit(main())
