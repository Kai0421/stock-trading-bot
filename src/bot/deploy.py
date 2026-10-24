# Author: Kai0421
"""The guarded upgrade of the live bot: ``check`` and ``go`` (docs/24-hot-upgrade.md).

Replaces the hand-run deploy script. Everything the upgrade reads or does goes through
``DeployEnv`` and a ``Switcher`` so the whole flow is tested with fakes; the real wiring is
``scripts/deploy_live.py``. Nothing here places an order, and nothing in the order path
imports this module.

Two ways to switch:

- ``InplaceSwitcher``: ``git pull`` in the live folder (today's layout). Needs a flat account.
- ``ReleaseSwitcher``: each version in its own folder with an atomic ``current`` pointer.
  Allows an upgrade while positions are open, if every one has its stop at the broker and the
  new version is shown (shadow tick) to plan exactly the same actions for them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from bot.journal import Journal
from bot.release import (
    BASELINE_VERSION,
    SHARED_DIRS,
    ReleaseError,
    ReleaseSet,
    TickRunning,
    freeze_open_positions,
    link_dir,
    link_shared,
    record_switch,
    window_blocker,
)
from bot.shadowtick import Snapshot, diff_intents, flat_blockers, hot_blockers

MAX_TICK_AGE_MIN = 15
TASK_RUNNING = 267009  # Task Scheduler's result while a run is still in progress
FLAG_WORDS = {
    "paused": "the bot is paused",
    "halt_today": "buying is halted for today",
    "halt_day": "buying is halted for the day after a big loss",
}


@dataclass(frozen=True)
class TickInfo:
    result: int | None
    last_run: datetime | None
    minutes_ago: int | None


def new_tick_finished(before: TickInfo, now: TickInfo) -> bool:
    """A tick has run since ``before`` and is no longer running. Windows moves "last run" to the
    moment a run starts, while its result still says "running" until it ends."""
    return now.last_run != before.last_run and now.result != TASK_RUNNING


def parse_tick_info(text: str, now: datetime | None = None) -> TickInfo:
    """``<result>|<last run as ISO UTC>`` from the Task Scheduler query."""
    parts = text.strip().split("|")
    try:
        result = int(parts[0])
        last = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
    except (ValueError, IndexError):
        return TickInfo(None, None, None)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return TickInfo(result, last, round((ref - last).total_seconds() / 60))


def read_rules_version(folder: Path | str) -> str:
    """The rules version a release folder carries (a release without stamping is baseline)."""
    f = Path(folder) / "src" / "bot" / "release.py"
    if not f.exists():
        return BASELINE_VERSION
    m = re.search(r'^RULES_VERSION\s*=\s*"([^"]+)"', f.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else BASELINE_VERSION


# ------------------------------------------------------------------ switchers


def _run(args: list[str], cwd: Path | str | None = None) -> str:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        raise ReleaseError(f"{' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


class Switcher(Protocol):
    name: str
    supports_hot: bool

    def outgoing_version(self) -> str: ...
    def current_code(self) -> Path | None: ...
    def prepare(self, target: str) -> Path: ...
    def activate(self, prepared: Path) -> None: ...
    def rollback(self) -> None: ...


class InplaceSwitcher:
    """Today's layout: ``git pull --ff-only`` in the live folder. No hot upgrades."""

    name = "in place"
    supports_hot = False
    activates_in_place = True

    def __init__(
        self,
        repo: Path,
        run: Callable[..., str] = _run,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.repo, self._run, self._today = Path(repo), run, today
        self._tag: str | None = None

    def outgoing_version(self) -> str:
        return read_rules_version(self.repo)

    def current_code(self) -> Path:
        return self.repo

    def prepare(self, target: str) -> Path:
        self._run(["git", "fetch", "-q", "origin"], cwd=self.repo)
        self._run(["git", "merge-base", "--is-ancestor", "HEAD", target], cwd=self.repo)
        self._tag = f"live-{self._today():%Y-%m-%d}"
        self._run(["git", "tag", "-f", self._tag, "HEAD"], cwd=self.repo)
        return self.repo

    def activate(self, prepared: Path) -> None:
        self._run(["git", "pull", "-q", "--ff-only", "origin", "main"], cwd=self.repo)
        self._run(["uv", "sync", "--frozen", "-q"], cwd=self.repo)

    def incoming_version(self, prepared: Path, target: str = "origin/main") -> str:
        try:
            text = self._run(["git", "show", f"{target}:src/bot/release.py"], cwd=self.repo)
        except ReleaseError:
            return BASELINE_VERSION
        m = re.search(r'^RULES_VERSION\s*=\s*"([^"]+)"', text, re.M)
        return m.group(1) if m else BASELINE_VERSION

    def rollback(self) -> None:
        if not self._tag:
            raise ReleaseError("no rollback tag was made")
        self._run(["git", "checkout", "-q", self._tag], cwd=self.repo)
        self._run(["uv", "sync", "--frozen", "-q"], cwd=self.repo)


class ReleaseSwitcher:
    """One folder per version under ``<root>/releases`` and an atomic ``current`` pointer.

    Shared state (data, logs, memory and .env) lives once in ``<root>/shared``.
    """

    name = "release folders"
    supports_hot = True
    activates_in_place = False

    def __init__(
        self,
        root: Path,
        source: Path,
        setup: Callable[[Path], None] | None = None,
        today: Callable[[], date] = date.today,
        run: Callable[..., str] = _run,
    ) -> None:
        self.root, self.source, self._today, self._run = Path(root), Path(source), today, run
        self._setup = setup or (
            lambda folder: self._run(["uv", "sync", "--frozen", "-q"], cwd=folder)
        )
        self.releases = ReleaseSet(self.root)

    def _uv_sync(self, folder: Path) -> None:
        self._run(["uv", "sync", "--frozen", "-q"], cwd=folder)

    def outgoing_version(self) -> str:
        cur = self.releases.current()
        return read_rules_version(cur) if cur else BASELINE_VERSION

    def current_code(self) -> Path | None:
        return self.releases.current()

    def prepare(self, target: str) -> Path:
        if self._run(["git", "remote"], cwd=self.source):
            self._run(["git", "fetch", "-q", "origin"], cwd=self.source)
        sha = self._run(["git", "rev-parse", "--short=7", target], cwd=self.source)
        dest = self.releases.releases_dir / f"{self._today():%Y-%m-%d}-{sha}"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._run(["git", "worktree", "add", "--detach", str(dest), sha], cwd=self.source)
        link_shared(dest, self.root / "shared")
        self._setup(dest)
        return dest

    def activate(self, prepared: Path) -> None:
        self.releases.point_to(prepared, note="upgrade")

    def rollback(self) -> None:
        self.releases.rollback()

    def prune(self, keep: int = 3) -> list[Path]:
        """Remove old release folders (never the current or previous one, never shared state)."""
        import os

        folders = sorted(
            (p for p in self.releases.releases_dir.glob("*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        protected = {self.releases.current(), self.releases.previous(), *folders[:keep]}
        removed = []
        for p in folders:
            if p in protected:
                continue
            for name in ("data", "logs", "memory"):  # unlink first: never touch shared state
                link = p / name
                if link.is_symlink() or (link.exists() and _is_link(link)):
                    os.rmdir(link) if os.name == "nt" else os.unlink(link)
            self._run(["git", "worktree", "remove", "--force", str(p)], cwd=self.source)
            removed.append(p)
        return removed


class FirstReleaseSwitcher(ReleaseSwitcher):
    """The one-time move from the old in-place live folder to the release layout.

    There is no ``current`` release yet, so the old live folder stands in as "current" for the
    shadow comparison. The switch is the ``tick`` task being pointed at the layout's launcher
    (``repoint``); going back points it at the old folder again (``restore``). The layout's
    ``shared`` folders must be links to the live folder's own ``data``, ``logs`` and ``memory``
    (see ``scripts/deploy_live.py``), so there is still exactly one journal and one run lock,
    and the old folder keeps working for the jobs that stay there.
    """

    name = "first move to release folders"

    def __init__(
        self,
        root: Path,
        source: Path,
        old_live: Path,
        repoint: Callable[[Path], None],
        restore: Callable[[], None],
        **kw: Any,
    ) -> None:
        super().__init__(root, source, **kw)
        self.old_live, self._repoint, self._restore = Path(old_live), repoint, restore
        self._switched = False

    def outgoing_version(self) -> str:
        cur = self.releases.current()
        return read_rules_version(cur or self.old_live)

    def current_code(self) -> Path | None:
        return self.releases.current() or self.old_live

    def activate(self, prepared: Path) -> None:
        launcher = self.root / "run_current.cmd"
        shutil.copyfile(prepared / "scripts" / "run_current.cmd", launcher)
        self.releases.point_to(prepared, note="first move to release folders")
        self._switched = True
        self._repoint(launcher)

    def rollback(self) -> None:
        self._restore()  # the tick task goes back to the old live folder; nothing else moved
        self._switched = False
        for name in ("current.txt", "previous.txt"):  # the layout is empty again
            (self.root / name).unlink(missing_ok=True)


def link_first_layout(root: Path, live: Path) -> None:
    """Make ``<root>/shared`` point at the live folder's own data, logs and memory (links, never
    copies) and copy the live ``.env`` beside them, so the first move shares the one journal and
    the one run lock with the old folder."""
    root, live = Path(root).resolve(), Path(live).resolve()
    shared = root / "shared"
    if live == root or live in root.parents or root in live.parents:
        raise ReleaseError("the layout root and the live folder must be separate folders")
    for name in SHARED_DIRS:
        if not (live / name).is_dir():
            raise ReleaseError(f"the live folder has no {name} folder at {live / name}")
        link_dir(shared / name, live / name)
    src = live / ".env"
    if not src.exists():
        raise ReleaseError(f"no .env in the live folder at {src}")
    shared.mkdir(parents=True, exist_ok=True)
    (shared / ".env").write_bytes(src.read_bytes())  # copy only; the live .env is never edited


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(p, "is_junction", lambda: False)())


# ------------------------------------------------------------------ the upgrade


@dataclass
class DeployEnv:
    """Everything the upgrade reads or does (the real wiring is scripts/deploy_live.py)."""

    now: Callable[[], datetime]
    session: Callable[[], tuple[datetime | None, datetime | None]]
    broker_snapshot: Callable[[], Snapshot]
    journal: Callable[[], Journal]
    tick_info: Callable[[], TickInfo]
    wait_next_tick: Callable[[TickInfo], TickInfo]
    health_first_line: Callable[[], str]
    trading_mode: Callable[[], str]
    flags: Callable[[], Mapping[str, Any]]
    shadow_intents: Callable[[Path, Snapshot], dict]
    check_release: Callable[[Path], list[str]]
    backup_db: Callable[[str], str]
    notify: Callable[[str, str, str], None]
    switch_lock: Callable[[], AbstractContextManager]
    describe_change: Callable[[], str]


@dataclass
class Result:
    status: str  # blocked | aborted | rolled_back | done
    lines: list[str] = field(default_factory=list)


def _set_flags(env: DeployEnv) -> list[str]:
    return [FLAG_WORDS.get(k, k.replace("_", " ")) for k, v in env.flags().items() if v]


def check(switcher: Switcher, env: DeployEnv, hot: bool = False) -> list[str]:
    """Why an upgrade must not go ahead right now (empty means it may)."""
    out: list[str] = []
    open_at, close_at = env.session()
    why = window_blocker(env.now(), open_at, close_at)
    if why:
        out.append(f"it is not a safe moment: {why}")
    tick = env.tick_info()
    if tick.result != 0:
        out.append(f"the last scheduled tick did not finish cleanly (result {tick.result})")
    elif tick.minutes_ago is None or tick.minutes_ago > MAX_TICK_AGE_MIN:
        out.append(
            f"the last scheduled tick ran {tick.minutes_ago} minutes ago, so the bot may not "
            "be running"
        )
    out += [f"{w}" for w in _set_flags(env)]
    snap = env.broker_snapshot()
    rows = env.journal().positions()
    if hot:
        if not switcher.supports_hot:
            out.append(
                "upgrading while holding positions needs the release layout, which is not set up"
            )
        else:
            out += hot_blockers(snap.positions, snap.orders, rows, env.now())
    else:
        out += flat_blockers(snap.positions, snap.orders)
    return out


def _holdings(snap: Snapshot) -> str:
    return ", ".join(f"{s} ({p.qty} shares)" for s, p in sorted(snap.positions.items()))


def go(
    switcher: Switcher, env: DeployEnv, hot: bool = False, target: str = "origin/main"
) -> Result:
    blockers = check(switcher, env, hot)
    if blockers:
        return Result("blocked", blockers)

    snap = env.broker_snapshot()
    outgoing = switcher.outgoing_version()
    mode_before = env.trading_mode()
    if snap.positions:
        state = (
            f"The account holds {_holdings(snap)}. Every position has its stop at the broker, "
            "and those stops stay in place during the upgrade. Before switching I check that "
            "the new version would handle each of them exactly the same way; if not, nothing "
            "changes."
        )
    else:
        state = "The account is flat: no positions and no open orders."
    env.notify(
        "INFO",
        ":rocket: Bot upgrade starting",
        f"Upgrading the bot ({env.describe_change()}). {state} The last scheduled tick was "
        "healthy and trading settings are not touched. I'll post again when the upgrade is "
        "done and checked.",
    )

    def abort(lines: list[str]) -> Result:
        env.notify(
            "WARN",
            ":warning: Bot upgrade stopped",
            "Nothing was changed: the bot is still on the previous version, and its positions "
            "and stops are untouched. Why: " + "; ".join(lines) + ".",
        )
        return Result("aborted", lines)

    try:
        prepared = switcher.prepare(target)
    except ReleaseError as exc:
        return abort([f"the new version could not be built ({exc})"])
    in_place = getattr(switcher, "activates_in_place", False)
    if not in_place:
        problems = env.check_release(prepared)
        if problems:
            return abort(problems)
    incoming = getattr(switcher, "incoming_version", None)
    new_version = incoming(prepared, target) if incoming else read_rules_version(prepared)

    if hot:
        if not (prepared / "src" / "bot" / "release.py").exists():
            return abort(
                ["the new version cannot remember which rules each open trade was opened under"]
            )
        current = switcher.current_code()
        if current is None:
            return abort(["there is no current release to compare against"])
        try:
            differences = diff_intents(
                env.shadow_intents(current, snap), env.shadow_intents(prepared, snap)
            )
        except Exception as exc:  # a release we cannot run read-only is not safe to switch to
            return abort([f"the comparison could not be run ({exc})"])
        if differences:
            return abort(differences)

    again = check(switcher, env, hot)
    if again:
        return abort(["things changed while the new version was being built: " + again[0]])

    try:
        with env.switch_lock():
            backup = env.backup_db(f"{env.now():%Y-%m-%d}")
            journal = env.journal()
            freeze_open_positions(journal, outgoing)
            record_switch(journal, new_version, env.now())
            before = env.tick_info()
            switcher.activate(prepared)
    except TickRunning:
        return abort(["a bot job was running at the moment of the switch; try again shortly"])

    after = env.wait_next_tick(before)
    problems = []
    if after.result != 0 or after.last_run == before.last_run:
        problems.append(f"the next scheduled tick did not run cleanly (result {after.result})")
    problems += [f"a safety flag is set: {w}" for w in _set_flags(env)]
    first = env.health_first_line()
    if not first.startswith("[INFO]"):
        problems.append(f"the health report says: {first}")
    if env.trading_mode() != mode_before:
        problems.append("the trading mode changed")
    if in_place:
        problems += env.check_release(prepared)
    if hot:
        now_snap = env.broker_snapshot()
        problems += hot_blockers(
            now_snap.positions, now_snap.orders, env.journal().positions(), env.now()
        )

    if problems:
        switcher.rollback()
        recheck = env.wait_next_tick(after)
        ok = recheck.result == 0
        env.notify(
            "CRITICAL",
            ":rotating_light: Bot upgrade needs attention",
            "The upgrade did not pass its checks, so the bot went back to the previous version "
            "by itself. Open positions and their stops were not changed. Problems: "
            + "; ".join(problems)
            + ". "
            + (
                "The next scheduled run after going back finished cleanly."
                if ok
                else "The run after going back did not finish cleanly either, so please check "
                "the bot."
            ),
        )
        return Result("rolled_back", problems)

    kept = (
        f" The open positions ({', '.join(sorted(snap.positions))}) kept their stops and the "
        "rules they were opened under."
        if snap.positions
        else ""
    )
    env.notify(
        "INFO",
        ":white_check_mark: Bot upgrade complete: all green",
        f"The bot is now on the new version (rules {new_version}). Checks passed: the code "
        f"loads, the next scheduled run finished cleanly, the health report is OK and no "
        f"safety flag is set. Trading mode unchanged ({mode_before.split('=')[-1]}), paper "
        f"trading.{kept} Going back is one step; the journal was backed up first "
        f"({backup}).",
    )
    return Result("done", [f"switched to rules {new_version}"])
