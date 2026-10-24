# Author: Kai0421
"""Building blocks for upgrading the bot while trades are open (docs/24-hot-upgrade.md).

Nothing here talks to the broker, and nothing in the order path imports this module, so it
cannot change a decision, an order or a stop. It provides:

- ``window_blocker``: when a switch is allowed (clear of the open, the close and the stop swap);
- ``ReleaseSet``: one folder per version plus an atomic ``current`` pointer, with rollback;
- ``SwitchLock``: the engine's own tick lock, held for the instant of a switch;
- version stamps: each open position remembers which rules version opened it (kv table only,
  so old code reads the same database unchanged);
- ``link_dir`` / ``sync_env``: keep data, logs, memory and ``.env`` in one shared place.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from bot.filelock import try_lock
from bot.journal import Journal

BASELINE_VERSION = "r1"  # how the bot behaves today
RULES_VERSION = "r1"  # bump (and register the new exit set) when a rule change ships
CLEAR_FROM_OPEN = timedelta(minutes=30)  # entries run 09:45-10:59; stay clear of them
CLEAR_FROM_CLOSE = timedelta(minutes=30)  # end-of-day jobs, and the overnight buy near 15:50
SHARED_DIRS = ("data", "logs", "memory")


class ReleaseError(RuntimeError):
    pass


class TickRunning(ReleaseError):
    """A bot job holds the lock, so a switch must wait."""


# ------------------------------------------------------------------ when a switch is allowed


def window_blocker(
    now: datetime, open_at: datetime | None, close_at: datetime | None
) -> str | None:
    """Why a switch must wait right now, in plain English, or None if it may go ahead.

    ``open_at`` / ``close_at`` come from the broker's calendar, so early closes are respected.
    """
    if open_at is None or close_at is None:
        return "the market is closed today"
    if now < open_at + CLEAR_FROM_OPEN:
        return (
            "it is too close to the market open: the bot is swapping stops and placing "
            "the day's buys"
        )
    if now > close_at - CLEAR_FROM_CLOSE:
        return "it is too close to the close: the end-of-day jobs are about to run"
    return None


# ------------------------------------------------------------------ release pointer


class ReleaseSet:
    """``<root>/releases/<tag>`` folders and the ``current.txt`` / ``previous.txt`` pointers."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def releases_dir(self) -> Path:
        return self.root / "releases"

    def _read(self, name: str) -> Path | None:
        f = self.root / name
        text = f.read_text(encoding="utf-8").strip() if f.exists() else ""
        return Path(text) if text else None

    def _write(self, name: str, value: Path) -> None:
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(str(value), encoding="utf-8")
        os.replace(tmp, self.root / name)  # atomic: a reader sees the old or the new, never half

    def current(self) -> Path | None:
        return self._read("current.txt")

    def previous(self) -> Path | None:
        return self._read("previous.txt")

    def point_to(self, release: Path | str, now: datetime | None = None, note: str = "") -> None:
        release = Path(release)
        if not release.is_dir():
            raise ReleaseError(f"release folder {release} does not exist")
        self.root.mkdir(parents=True, exist_ok=True)
        old = self.current()
        if old is not None and old != release:
            self._write("previous.txt", old)
        self._write("current.txt", release)
        entry = {
            "at": (now or datetime.now().astimezone()).isoformat(),
            "from": str(old) if old else None,
            "to": str(release),
            "note": note,
        }
        with open(self.root / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def rollback(self, now: datetime | None = None) -> Path:
        prev = self.previous()
        if prev is None:
            raise ReleaseError("there is no previous release to go back to")
        self.point_to(prev, now=now, note="rollback")
        return prev


# ------------------------------------------------------------------ tick lock


class SwitchLock:
    """Hold the engine's job lock for the instant of a switch.

    The engine takes the same file lock for every job and skips (harmlessly, until the next
    five-minute tick) when it cannot get it. So no job runs during a switch, and a switch never
    starts while a job is running.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh: IO[Any] | None = None

    def __enter__(self) -> SwitchLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "w")
        if not try_lock(fh):
            fh.close()
            raise TickRunning("a bot job is running right now")
        self._fh = fh
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ------------------------------------------------------------------ rules version stamps


def _stamp_key(symbol: str) -> str:
    return f"posver:{symbol}"


def freeze_open_positions(journal: Journal, version: str) -> list[str]:
    """Remember which rules version each open position was opened under (never overwrites).

    Stored as ``version|entry_date`` so a later trade in the same stock cannot inherit it.
    """
    stamped = []
    for symbol, row in journal.positions().items():
        if journal.get_kv(_stamp_key(symbol)) is None:
            journal.set_kv(_stamp_key(symbol), f"{version}|{row.entry_date.isoformat()}")
            stamped.append(symbol)
    return stamped


def record_switch(journal: Journal, version: str, at: datetime) -> None:
    switches = json.loads(journal.get_kv("rules_switches") or "[]")
    switches.append({"version": version, "at": at.isoformat()})
    journal.set_kv("rules_switches", json.dumps(switches))


def rules_version_of(journal: Journal, symbol: str, entry_date: date) -> str:
    """The rules a position was opened under: its stamp, else the latest switch, else baseline.

    New code uses this to apply new rules to new entries only; an open position keeps the exit
    rules it was opened under.
    """
    stamp = journal.get_kv(_stamp_key(symbol))
    if stamp:
        version, _, stamped_on = stamp.partition("|")
        if stamped_on == entry_date.isoformat():
            return version
    switches = json.loads(journal.get_kv("rules_switches") or "[]")
    return switches[-1]["version"] if switches else BASELINE_VERSION


# ------------------------------------------------------------------ shared state


def link_dir(link: Path | str, target: Path | str) -> None:
    """Make ``link`` a directory link to ``target`` (a junction on Windows, no admin needed).

    An existing real folder at ``link`` (for example a tracked ``memory/``) is removed first
    only if it is not already a link; its files are never copied into the shared state.
    """
    link, target = Path(link), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or (sys.platform == "win32" and _is_junction(link)):
        _remove_link(link)
    elif link.exists():
        _rmtree(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True
        )
        if done.returncode != 0:
            raise ReleaseError(
                f"could not link {link}: {done.stderr.strip() or done.stdout.strip()}"
            )
    else:
        os.symlink(target, link, target_is_directory=True)


def _is_junction(p: Path) -> bool:
    return bool(getattr(p, "is_junction", lambda: False)())


def _remove_link(p: Path) -> None:
    os.rmdir(p) if sys.platform == "win32" else os.unlink(p)  # removes the link, not the target


def _rmtree(p: Path) -> None:
    import shutil

    shutil.rmtree(p)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sync_env(release: Path | str, shared: Path | str) -> None:
    """Copy the one shared ``.env`` into a release folder and check the copy is identical."""
    release, shared = Path(release), Path(shared)
    src = shared / ".env"
    if not src.exists():
        raise ReleaseError(f"no shared .env at {src}")
    (release / ".env").write_bytes(src.read_bytes())
    if not env_matches(release, shared):
        raise ReleaseError("the .env copy in the release does not match the shared one")


def env_matches(release: Path | str, shared: Path | str) -> bool:
    a, b = Path(release) / ".env", Path(shared) / ".env"
    return a.exists() and b.exists() and _sha(a) == _sha(b)


def link_shared(release: Path | str, shared: Path | str) -> None:
    """Point a release's data, logs and memory at the shared folders and copy the shared .env."""
    release, shared = Path(release), Path(shared)
    for name in SHARED_DIRS:
        link_dir(release / name, shared / name)
    sync_env(release, shared)
