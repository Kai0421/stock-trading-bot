# Author: Kai0421
r"""Guarded upgrade of the live bot (docs/24-hot-upgrade.md). Replaces the hand-run script.

    uv run python scripts/deploy_live.py check                 can an upgrade go ahead right now?
    uv run python scripts/deploy_live.py go                    upgrade (account must be flat)
    uv run python scripts/deploy_live.py go --holding          upgrade while positions are open
                                                               (release layout only)
    uv run python scripts/deploy_live.py layout --root R       link R\shared to the live folder
    uv run python scripts/deploy_live.py go --holding --first --root R --source S
                                                               the one-time move to the layout

Run it from an up-to-date checkout of main, not from the live folder. ``--live`` is the live
bot's folder (default layout: ``git pull`` there); ``--root`` selects the release layout.
Posts "Bot upgrade starting" and "complete: all green" (or a CRITICAL note) to Slack, checks the
next scheduled tick and the health report, and goes back by itself if anything is off.
Never edits .env, never places or cancels an order.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

from bot.broker import Broker  # noqa: E402
from bot.calendar import MarketCalendar  # noqa: E402
from bot.config import Settings, load_env_file  # noqa: E402
from bot.deploy import (  # noqa: E402
    DeployEnv,
    FirstReleaseSwitcher,
    InplaceSwitcher,
    ReleaseSwitcher,
    TickInfo,
    check,
    go,
    link_first_layout,
    new_tick_finished,
    parse_tick_info,
)
from bot.journal import Journal  # noqa: E402
from bot.release import SwitchLock  # noqa: E402
from bot.shadowtick import ReadOnlyBroker, snapshot, snapshot_to_json  # noqa: E402

ET = ZoneInfo("America/New_York")
TASK = r"\StockTradingBot" + "\\"
KEY_TESTS = [
    "tests/test_health.py", "tests/test_overnight_engine.py",
    "tests/test_layer_equivalence.py", "tests/test_engine.py",
]  # fmt: skip


def sh(args: list[str], cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def tick_info() -> TickInfo:
    ps = (
        f"$i=Get-ScheduledTaskInfo -TaskPath '{TASK}' -TaskName tick; "
        "\"$($i.LastTaskResult)|$($i.LastRunTime.ToUniversalTime().ToString('o'))\""
    )
    out = sh(["powershell", "-NoProfile", "-Command", ps]).stdout
    return parse_tick_info(out)


def tick_action() -> tuple[str, str]:
    """The tick task's current program and arguments (so the first move can go back)."""
    ps = (
        f"$a=(Get-ScheduledTask -TaskPath '{TASK}' -TaskName tick).Actions[0]; "
        '"$($a.Execute)|$($a.Arguments)"'
    )
    out = sh(["powershell", "-NoProfile", "-Command", ps]).stdout.strip()
    execute, _, args = out.partition("|")
    return execute, args


def set_tick_action(execute: str, args: str) -> None:
    ps = (
        f"$a=New-ScheduledTaskAction -Execute '{execute}' -Argument '{args}'; "
        f"Set-ScheduledTask -TaskPath '{TASK}' -TaskName tick -Action $a | Out-Null"
    )
    done = sh(["powershell", "-NoProfile", "-Command", ps])
    if done.returncode != 0:
        raise RuntimeError(f"could not change the tick task: {done.stderr.strip()}")


def build(args: argparse.Namespace) -> tuple[object, DeployEnv]:
    layout_root = Path(args.root) if args.root else None
    live = Path(args.live)
    if layout_root:
        shared = layout_root / "shared"
        if args.first:
            old_execute, old_args = tick_action()
            switcher = FirstReleaseSwitcher(
                layout_root,
                Path(args.source or HERE),
                live,
                repoint=lambda launcher: set_tick_action("cmd.exe", f'/c ""{launcher}" tick"'),
                restore=lambda: set_tick_action(old_execute, old_args),
            )
        else:
            switcher = ReleaseSwitcher(layout_root, Path(args.source or HERE))
        db, env_file = shared / "data" / "bot.db", shared / ".env"

        def code_dir() -> Path:
            return switcher.current_code() or live
    else:
        switcher = InplaceSwitcher(live)
        db, env_file = live / "data" / "bot.db", live / ".env"

        def code_dir() -> Path:
            return live

    def broker() -> ReadOnlyBroker:
        s = Settings.from_env({**load_env_file(env_file), **os.environ})
        return ReadOnlyBroker(Broker.from_settings(s))

    def session():
        b = broker()
        if not b.clock().is_open:
            return None, None
        s = MarketCalendar().session_for(datetime.now(ET).date())
        return (s.open, s.close) if s else (None, None)

    def wait_next(before: TickInfo) -> TickInfo:
        for _ in range(80):  # up to about 13 minutes
            time.sleep(10)
            now = tick_info()
            if new_tick_finished(before, now):
                return now
        return tick_info()

    def health() -> str:
        out = sh(["uv", "run", "python", "scripts/health_report.py", "--print"], cwd=code_dir())
        return (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else ""

    def mode() -> str:
        for line in env_file.read_text().splitlines():
            if line.startswith("TRADING_MODE="):
                return line.strip()
        return "TRADING_MODE=(unset)"

    def intents(folder: Path, snap) -> dict:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            copy = tmp / "bot.db"
            src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            dst = sqlite3.connect(copy)
            src.backup(dst)
            src.close()
            dst.close()
            (tmp / "snap.json").write_text(json.dumps(snapshot_to_json(snap)))
            done = sh(
                [sys.executable, str(HERE / "scripts/shadow_tick.py"), "--code", str(folder),
                 "intents", "--db", str(copy), "--snapshot", str(tmp / "snap.json"),
                 "--out", str(tmp / "out.json")],
                cwd=HERE,
            )  # fmt: skip
            if done.returncode != 0:
                raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1])
            return json.loads((tmp / "out.json").read_text())

    def check_release(folder: Path) -> list[str]:
        problems = []
        mods = "bot.engine, bot.execute, bot.overnight_live, bot.health"
        if sh(["uv", "run", "python", "-c", f"import {mods}"], cwd=folder).returncode != 0:
            problems.append("the new version's code does not load")
        if sh(["uv", "run", "pytest", "-q", "-x", *KEY_TESTS], cwd=folder).returncode != 0:
            problems.append("the new version's key tests fail")
        return problems

    def backup(tag: str) -> str:
        dest = db.with_name(f"bot.db.bak-before-deploy-{tag}")
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        out = sqlite3.connect(dest)
        src.backup(out)
        src.close()
        out.close()
        return dest.name

    def notify(level: str, title: str, text: str) -> None:
        done = sh(["uv", "run", "python", "-m", "bot.notify", "--level", level, "--title", title,
                   text], cwd=code_dir())  # fmt: skip
        if done.returncode != 0:
            print("(Slack post failed)")

    def describe() -> str:
        if layout_root:
            return "a new version of the bot"
        sh(["git", "fetch", "-q", "origin"], cwd=live)
        n = sh(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=live).stdout.strip()
        return f"{n} new changes"

    env = DeployEnv(
        now=lambda: datetime.now(ET),
        session=session,
        broker_snapshot=lambda: snapshot(broker()),
        journal=lambda: Journal(db),
        tick_info=tick_info,
        wait_next_tick=wait_next,
        health_first_line=health,
        trading_mode=mode,
        flags=lambda: {k: f.value for k, f in Journal(db).flags().items()},
        shadow_intents=intents,
        check_release=check_release,
        backup_db=backup,
        notify=notify,
        switch_lock=lambda: SwitchLock(db.with_name("engine.lock")),
        describe_change=describe,
    )
    return switcher, env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["check", "go", "layout"])
    ap.add_argument(
        "--first",
        action="store_true",
        help="the one-time move from the old live folder to the release layout (needs --root)",
    )
    ap.add_argument("--holding", action="store_true", help="allow open positions (release layout)")
    ap.add_argument("--live", default=str(Path.home() / "Development" / "StockTradingBot"))
    ap.add_argument("--root", help="release layout root (releases/, shared/, current.txt)")
    ap.add_argument("--source", help="git checkout the releases are built from (default: here)")
    ap.add_argument("--target", default="origin/main")
    args = ap.parse_args()
    if args.command == "layout":
        if not args.root:
            raise SystemExit("layout needs --root")
        link_first_layout(Path(args.root), Path(args.live))
        print(rf"{args.root}\shared now points at the live folder's data, logs and memory")
        return 0
    if args.first and not args.root:
        raise SystemExit("--first needs --root")
    switcher, env = build(args)
    if args.command == "check":
        blockers = check(switcher, env, hot=args.holding)
        print("\n".join(f"BLOCKED: {b}" for b in blockers) or "all checks passed (check only)")
        return 1 if blockers else 0
    result = go(switcher, env, hot=args.holding, target=args.target)
    print(result.status.upper())
    print("\n".join(f"- {line}" for line in result.lines))
    return {"done": 0, "blocked": 1, "aborted": 1, "rolled_back": 3}[result.status]


if __name__ == "__main__":
    sys.exit(main())
