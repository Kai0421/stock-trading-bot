# Author: Kai0421
"""Which machine runs the bot: ``BOT_HOST=mac`` (launchd) or ``BOT_HOST=windows`` (Task Scheduler).

Unset means "this machine". When set, the engine refuses to run on a different OS, so a
``.env`` copied between the Mac and the Windows laptop can't make the wrong one trade.

Install the scheduler for the configured host (run on that machine; Windows needs an
administrator PowerShell for the hidden task):
    uv run python -m bot.host install [--overnight-shadow]
    uv run python -m bot.host uninstall [--overnight-shadow]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from bot.config import load_env_file

HOSTS = frozenset({"mac", "windows"})
REPO = Path(__file__).resolve().parents[2]


class HostError(ValueError):
    pass


def current_os(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return "mac"
    if platform.startswith("win"):
        return "windows"
    return "linux"


def configured_host(env: Mapping[str, str], platform: str | None = None) -> str:
    """BOT_HOST, or this machine's OS when unset."""
    raw = (env.get("BOT_HOST") or "").strip().lower()
    if not raw:
        return current_os(platform)
    if raw not in HOSTS:
        raise HostError(f"BOT_HOST must be one of {sorted(HOSTS)} (got {raw!r})")
    return raw


def host_mismatch(env: Mapping[str, str], platform: str | None = None) -> str | None:
    """Why this machine must not run the bot, or None when it may."""
    want = configured_host(env, platform)
    have = current_os(platform)
    if want == have:
        return None
    return f"BOT_HOST={want} but this machine is {have}; not running here"


def scheduler_command(
    host: str, repo: Path, *, uninstall: bool, overnight_shadow: bool
) -> list[str]:
    if host == "mac":
        cmd = ["/bin/bash", str(repo / "scripts" / "install_launchd.sh")]
        if overnight_shadow:
            cmd.append("--overnight-shadow")
        if uninstall:
            cmd.append("--uninstall")
        return cmd
    if host == "windows":
        script = repo / "scripts" / "install_windows_task.ps1"
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        if overnight_shadow:
            cmd.append("-OvernightShadow")
        if uninstall:
            cmd.append("-Uninstall")
        return cmd
    raise HostError(f"no scheduler for {host!r}; set BOT_HOST to one of {sorted(HOSTS)}")


def env_for(repo: Path) -> dict[str, str]:
    return {**load_env_file(repo / ".env"), **os.environ}


def main(argv: Sequence[str] | None = None, repo: Path = REPO) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.host")
    ap.add_argument("action", choices=["install", "uninstall", "show"])
    ap.add_argument("--overnight-shadow", action="store_true")
    args = ap.parse_args(argv)
    env = env_for(repo)
    try:
        host = configured_host(env)
        problem = host_mismatch(env)
        if args.action == "show":
            print(f"BOT_HOST={host} (this machine: {current_os()})")
            return 0
        if problem:
            raise HostError(problem)
        cmd = scheduler_command(
            host, repo, uninstall=args.action == "uninstall",
            overnight_shadow=args.overnight_shadow,
        )  # fmt: skip
    except HostError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
