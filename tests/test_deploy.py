# Author: Kai0421
"""The guarded upgrade: check / go, with the flat path, the hot path, abort and rollback."""

import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot.broker import BrokerOrder, BrokerPosition
from bot.deploy import (
    DeployEnv,
    InplaceSwitcher,
    ReleaseSwitcher,
    TickInfo,
    check,
    go,
    new_tick_finished,
    parse_tick_info,
    read_rules_version,
)
from bot.journal import Journal, PositionRow
from bot.release import ReleaseError, ReleaseSet, TickRunning, rules_version_of
from bot.shadowtick import Snapshot

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=ET)
OPEN, CLOSE = datetime(2026, 10, 15, 9, 30, tzinfo=ET), datetime(2026, 10, 15, 16, 0, tzinfo=ET)
BEFORE = TickInfo(0, datetime(2026, 10, 15, 11, 55, tzinfo=ET), 5)
AFTER = TickInfo(0, datetime(2026, 10, 15, 12, 0, tzinfo=ET), 0)


def stop_order(symbol="MSFT", qty=100):
    return BrokerOrder("s1", "s1-c", symbol, "sell", qty, 0, None, "stop", "simple", None,
                       96.0, "new", (), None, None)  # fmt: skip


def row(symbol="MSFT", qty=100):
    return PositionRow(symbol, 100.0, qty, qty, 96.0, 96.0, date(2026, 10, 14), 1, False, "sig",
                       0.8, "Information Technology", "s1", None)  # fmt: skip


def held(symbol="MSFT", qty=100):
    return BrokerPosition(symbol, qty, qty, 100.0, 101.0, "us_equity")


@dataclass
class FakeSwitcher:
    supports_hot: bool = True
    calls: list = field(default_factory=list)
    prepare_error: Exception | None = None
    version: str = "r1"
    folder: Path | None = None

    name = "fake"

    def outgoing_version(self):
        return self.version

    def current_code(self):
        return self.folder.parent / "current"

    def prepare(self, target):
        self.calls.append("prepare")
        if self.prepare_error:
            raise self.prepare_error
        return self.folder

    def activate(self, prepared):
        self.calls.append("activate")

    def rollback(self):
        self.calls.append("rollback")


@dataclass
class World:
    """Everything the upgrade reads or does, in one inspectable place."""

    tmp: Path
    positions: dict = field(default_factory=dict)
    orders: list = field(default_factory=list)
    rows: dict = field(default_factory=dict)
    now: datetime = NOON
    session: tuple = (OPEN, CLOSE)
    tick: TickInfo = BEFORE
    after_tick: TickInfo = AFTER
    flags: dict = field(default_factory=dict)
    health: str = "[INFO] Morning health: OK"
    mode: str = "TRADING_MODE=swing"
    mode_after: str | None = None
    release_problems: list = field(default_factory=list)
    new_intents: dict | None = None
    lock_busy: bool = False
    messages: list = field(default_factory=list)
    events: list = field(default_factory=list)
    snapshots_taken: int = 0
    change_after_prepare: bool = False

    def __post_init__(self):
        self.journal = Journal(self.tmp / "bot.db")
        for r in self.rows.values():
            self.journal.upsert_position(r)

    def env(self) -> DeployEnv:
        w = self

        def snapshot():
            w.snapshots_taken += 1
            return Snapshot(dict(w.positions), list(w.orders))

        def intents(folder, snap):
            base = {"protect": [], "exits": [], "stop_changes": [], "cancel_entries": []}
            if folder.name != "current" and w.new_intents is not None:
                return w.new_intents
            return base

        @contextmanager
        def lock():
            if w.lock_busy:
                raise TickRunning("busy")
            w.events.append("lock")
            yield
            w.events.append("unlock")

        def wait(before):
            w.events.append("waited")
            if w.mode_after:
                w.mode = w.mode_after
            return w.after_tick

        return DeployEnv(
            now=lambda: w.now,
            session=lambda: w.session,
            broker_snapshot=snapshot,
            journal=lambda: w.journal,
            tick_info=lambda: w.tick,
            wait_next_tick=wait,
            health_first_line=lambda: w.health,
            trading_mode=lambda: w.mode,
            flags=lambda: dict(w.flags),
            shadow_intents=intents,
            check_release=lambda folder: list(w.release_problems),
            backup_db=lambda tag: w.events.append("backup") or f"bot.db.bak-{tag}",
            notify=lambda level, title, text: w.messages.append((level, title, text)),
            switch_lock=lock,
            describe_change=lambda: "3 new changes",
        )


def make(tmp_path, hot=False, **kw):
    folder = tmp_path / "rel"
    (folder / "src" / "bot").mkdir(parents=True)
    (folder / "src" / "bot" / "release.py").write_text('RULES_VERSION = "r2"\n')
    w = World(tmp_path, **kw)
    sw = FakeSwitcher(folder=folder)
    return w, sw, w.env()


# ------------------------------------------------------------------ check


def test_check_passes_when_flat_inside_the_window(tmp_path):
    w, sw, env = make(tmp_path)
    assert check(sw, env, hot=False) == []


@pytest.mark.parametrize(
    "kw, word",
    [
        ({"now": datetime(2026, 10, 15, 9, 40, tzinfo=ET)}, "open"),
        ({"session": (None, None)}, "closed"),
        ({"tick": TickInfo(1, BEFORE.last_run, 5)}, "tick"),
        ({"tick": TickInfo(0, BEFORE.last_run, 40)}, "tick"),
        ({"flags": {"paused": True}}, "paused"),
        ({"positions": {"MSFT": held()}, "rows": {"MSFT": row()}}, "MSFT"),
    ],
)
def test_check_blocks_in_plain_english(tmp_path, kw, word):
    w, sw, env = make(tmp_path, **kw)
    blockers = check(sw, env, hot=False)
    assert blockers and any(word in b for b in blockers)
    assert all(not re.search(r"[a-z]+_[a-z]+", b) for b in blockers)


def test_hot_check_allows_protected_positions(tmp_path):
    w, sw, env = make(
        tmp_path,
        positions={"MSFT": held()},
        orders=[stop_order()],
        rows={"MSFT": row()},
    )
    assert check(sw, env, hot=True) == []
    assert check(sw, env, hot=False) != []  # the normal path still wants a flat account


def test_hot_check_refuses_an_unprotected_position(tmp_path):
    w, sw, env = make(tmp_path, positions={"MSFT": held()}, orders=[], rows={"MSFT": row()})
    assert any("stop" in b for b in check(sw, env, hot=True))


def test_hot_needs_a_switcher_that_supports_it(tmp_path):
    w, sw, env = make(tmp_path, positions={"MSFT": held()}, orders=[stop_order()],
                      rows={"MSFT": row()})  # fmt: skip
    sw.supports_hot = False
    assert any("release layout" in b for b in check(sw, env, hot=True))


# ------------------------------------------------------------------ go


def test_flat_upgrade_is_green_and_posts_start_and_finish(tmp_path):
    w, sw, env = make(tmp_path)
    res = go(sw, env, hot=False)
    assert res.status == "done"
    assert sw.calls == ["prepare", "activate"]
    assert w.events.index("backup") < w.events.index("waited")
    assert [m[1] for m in w.messages] == [
        ":rocket: Bot upgrade starting",
        ":white_check_mark: Bot upgrade complete: all green",
    ]
    assert "flat" in w.messages[0][2]


def test_blocked_upgrade_changes_nothing_and_says_nothing(tmp_path):
    w, sw, env = make(tmp_path, now=datetime(2026, 10, 15, 9, 40, tzinfo=ET))
    res = go(sw, env, hot=False)
    assert res.status == "blocked" and sw.calls == [] and w.messages == []


def test_hot_upgrade_stamps_open_positions_and_records_the_switch(tmp_path):
    w, sw, env = make(
        tmp_path, positions={"MSFT": held()}, orders=[stop_order()], rows={"MSFT": row()}
    )
    res = go(sw, env, hot=True)
    assert res.status == "done", res.lines
    # the open position keeps the rules it was opened under, a new entry gets the new ones
    assert rules_version_of(w.journal, "MSFT", date(2026, 10, 14)) == "r1"
    assert rules_version_of(w.journal, "NVDA", date(2026, 10, 16)) == "r2"
    start = w.messages[0][2]
    assert "MSFT" in start and "100 shares" in start and "stop" in start


def test_hot_upgrade_is_refused_if_the_new_version_would_treat_a_position_differently(tmp_path):
    w, sw, env = make(
        tmp_path, positions={"MSFT": held()}, orders=[stop_order()], rows={"MSFT": row()},
        new_intents={"protect": [], "exits": [{"symbol": "MSFT", "kind": "profit_target",
                                               "qty": 100, "full": True, "cid": "c"}],
                     "stop_changes": [], "cancel_entries": []},
    )  # fmt: skip
    res = go(sw, env, hot=True)
    assert res.status == "aborted" and "activate" not in sw.calls
    assert any("MSFT" in line and "selling" in line for line in res.lines)
    assert w.messages[-1][0] == "WARN" and "nothing was changed" in w.messages[-1][2].lower()


def test_hot_upgrade_needs_stamping_in_the_new_version(tmp_path):
    w, sw, env = make(
        tmp_path, positions={"MSFT": held()}, orders=[stop_order()], rows={"MSFT": row()}
    )
    (sw.folder / "src" / "bot" / "release.py").unlink()
    res = go(sw, env, hot=True)
    assert res.status == "aborted" and "activate" not in sw.calls
    assert any("remember" in line or "stamp" in line for line in res.lines)


def test_a_failed_build_aborts_before_any_switch(tmp_path):
    w, sw, env = make(tmp_path)
    sw.prepare_error = ReleaseError("uv sync failed")
    res = go(sw, env, hot=False)
    assert res.status == "aborted" and sw.calls == ["prepare"]
    assert w.messages[-1][0] == "WARN"


def test_failed_pre_switch_checks_abort(tmp_path):
    w, sw, env = make(tmp_path, release_problems=["the key tests fail"])
    res = go(sw, env, hot=False)
    assert res.status == "aborted" and "activate" not in sw.calls


def test_a_job_running_at_the_moment_of_the_switch_aborts_cleanly(tmp_path):
    w, sw, env = make(tmp_path, lock_busy=True)
    res = go(sw, env, hot=False)
    assert res.status == "aborted" and "activate" not in sw.calls
    assert "backup" not in w.events


def test_state_that_changes_during_the_build_aborts(tmp_path):
    w, sw, env = make(tmp_path)
    original = sw.prepare

    def prepare_then_a_position_appears(target):
        out = original(target)
        w.positions["MSFT"] = held()
        w.rows["MSFT"] = row()
        return out

    sw.prepare = prepare_then_a_position_appears
    res = go(sw, env, hot=False)
    assert res.status == "aborted" and "activate" not in sw.calls


@pytest.mark.parametrize(
    "kw",
    [
        {"after_tick": TickInfo(1, AFTER.last_run, 0)},  # the next tick failed
        {"after_tick": BEFORE},  # no new tick ran
        {"health": "[WARN] Morning health: ATTENTION"},
        {"mode_after": "TRADING_MODE=overnight"},
        {"flags": {"halt_today": True}},
    ],
)
def test_a_bad_result_rolls_back_by_itself_and_says_so(tmp_path, kw):
    w, sw, env = make(tmp_path, **kw)
    if "flags" in kw:  # the flag appears only after the switch
        flags, w.flags = w.flags, {}
        orig = env.wait_next_tick

        def wait(before):
            w.flags = flags
            return orig(before)

        env.wait_next_tick = wait
    res = go(sw, env, hot=False)
    assert res.status == "rolled_back"
    assert sw.calls[-1] == "rollback"
    level, title, text = w.messages[-1]
    assert level == "CRITICAL" and "went back" in text
    assert "stops" in text and "unchanged" in text or "not changed" in text


def test_messages_are_plain_english(tmp_path):
    w, sw, env = make(
        tmp_path, positions={"MSFT": held()}, orders=[stop_order()], rows={"MSFT": row()}
    )
    go(sw, env, hot=True)
    for _, title, text in w.messages:
        assert not re.search(r"\b[a-z]+_[a-z]+\b", title + text)


# ------------------------------------------------------------------ helpers and switchers


def test_parse_tick_info_from_powershell_output():
    info = parse_tick_info("0|2026-10-15T15:55:01.0000000Z|7", now=datetime(2026, 10, 15, 16, 2))
    assert info.result == 0 and info.minutes_ago == 7


def test_read_rules_version(tmp_path):
    (tmp_path / "src" / "bot").mkdir(parents=True)
    assert read_rules_version(tmp_path) == "r1"  # a release without stamping is the baseline
    (tmp_path / "src" / "bot" / "release.py").write_text('RULES_VERSION = "r7"\n')
    assert read_rules_version(tmp_path) == "r7"


def test_release_switcher_builds_links_switches_and_rolls_back(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git = lambda *a, cwd=source: subprocess.run(  # noqa: E731
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
        cwd=cwd, check=True, capture_output=True, text=True,
    )  # fmt: skip
    git("init", "-q", "-b", "main")
    (source / "memory").mkdir()
    (source / "memory" / "README.md").write_text("tracked")
    (source / "app.txt").write_text("v1")
    git("add", "-A")
    git("commit", "-q", "-m", "one")
    root = tmp_path / "root"
    shared = root / "shared"
    for d in ("data", "logs", "memory"):
        (shared / d).mkdir(parents=True)
    (shared / ".env").write_text("TRADING_MODE=swing\n")

    sw = ReleaseSwitcher(root, source, setup=lambda folder: None, today=lambda: date(2026, 10, 15))
    first = sw.prepare("main")
    sw.activate(first)
    (source / "app.txt").write_text("v2")
    git("commit", "-q", "-am", "two")
    second = sw.prepare("main")
    assert first != second and (second / "app.txt").read_text() == "v2"
    (second / "data" / "bot.db").write_text("shared")
    assert (shared / "data" / "bot.db").read_text() == "shared"  # state is shared, not copied
    assert (second / ".env").read_text() == "TRADING_MODE=swing\n"
    sw.activate(second)
    assert ReleaseSet(root).current() == second
    sw.rollback()
    assert ReleaseSet(root).current() == first
    assert sw.supports_hot


def test_inplace_switcher_cannot_do_hot_upgrades():
    assert InplaceSwitcher(Path("."), run=lambda *a, **k: "").supports_hot is False


def test_inplace_switcher_reads_the_incoming_rules_version_from_git():
    sw = InplaceSwitcher(Path("."), run=lambda args, cwd=None: 'RULES_VERSION = "r3"\n')
    assert sw.incoming_version(Path(".")) == "r3"

    def no_file(args, cwd=None):
        raise ReleaseError("not there")

    assert InplaceSwitcher(Path("."), run=no_file).incoming_version(Path(".")) == "r1"


def test_prune_removes_old_releases_but_never_shared_state_or_current(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*a):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
            cwd=source, check=True, capture_output=True, text=True,
        )  # fmt: skip

    git("init", "-q", "-b", "main")
    (source / "app.txt").write_text("v0")
    git("add", "-A")
    git("commit", "-q", "-m", "c0")
    root = tmp_path / "root"
    for d in ("data", "logs", "memory"):
        (root / "shared" / d).mkdir(parents=True)
    (root / "shared" / "data" / "bot.db").write_text("precious")
    (root / "shared" / ".env").write_text("TRADING_MODE=swing\n")
    days = iter(range(1, 8))
    sw = ReleaseSwitcher(
        root, source, setup=lambda f: None, today=lambda: date(2026, 10, next(days))
    )
    made = []
    for i in range(1, 5):
        (source / "app.txt").write_text(f"v{i}")
        git("commit", "-q", "-am", f"c{i}")
        made.append(sw.prepare("main"))
        sw.activate(made[-1])
    removed = sw.prune(keep=1)
    assert made[0] in removed and made[3] not in removed  # newest/current/previous survive
    assert (root / "shared" / "data" / "bot.db").read_text() == "precious"
    assert sw.releases.current() == made[3] and made[2].exists()


# ------------------------------------------------------------------ the first move to the layout


def first_move(tmp_path, **world_kw):
    """A real git source with the launcher, an old live folder, and a switcher that records
    what the tick task is told to do."""
    from bot.deploy import FirstReleaseSwitcher, link_first_layout

    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "src" / "bot").mkdir(parents=True)
    (source / "scripts" / "run_current.cmd").write_text("@echo off\n")
    (source / "src" / "bot" / "release.py").write_text('RULES_VERSION = "r2"\n')
    git = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
        cwd=source, check=True, capture_output=True, text=True,
    )  # fmt: skip
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "one")
    live = tmp_path / "live"
    for d in ("data", "logs", "memory"):
        (live / d).mkdir(parents=True)
    (live / ".env").write_text("TRADING_MODE=swing\nPAPER=true\n")
    root = tmp_path / "root"
    link_first_layout(root, live)
    calls = []
    sw = FirstReleaseSwitcher(
        root, source, live,
        repoint=lambda launcher: calls.append(("repoint", launcher)),
        restore=lambda: calls.append(("restore",)),
        setup=lambda folder: None, today=lambda: date(2026, 10, 21),
    )  # fmt: skip
    w = World(tmp_path / "w", **world_kw) if (tmp_path / "w").mkdir() is None else None
    return w, sw, calls, root, live


def test_the_layout_shares_the_live_folders_state_and_copies_only_the_env(tmp_path):
    w, sw, calls, root, live = first_move(tmp_path)
    (root / "shared" / "data" / "bot.db").write_text("one journal")
    assert (live / "data" / "bot.db").read_text() == "one journal"  # a link, not a copy
    assert (root / "shared" / ".env").read_bytes() == (live / ".env").read_bytes()
    assert (live / ".env").read_text() == "TRADING_MODE=swing\nPAPER=true\n"  # untouched


def test_the_layout_refuses_nested_folders_and_a_missing_live_folder(tmp_path):
    from bot.deploy import link_first_layout

    live = tmp_path / "live"
    for d in ("data", "logs", "memory"):
        (live / d).mkdir(parents=True)
    (live / ".env").write_text("x")
    with pytest.raises(ReleaseError):
        link_first_layout(live / "inside", live)
    with pytest.raises(ReleaseError):
        link_first_layout(tmp_path / "root", tmp_path / "nowhere")


def test_first_move_uses_the_old_folder_as_current_until_the_switch(tmp_path):
    w, sw, calls, root, live = first_move(tmp_path)
    assert sw.current_code() == live and ReleaseSet(root).current() is None
    folder = sw.prepare("main")
    sw.activate(folder)
    assert sw.current_code() == folder
    assert (root / "run_current.cmd").read_text() == "@echo off\n"
    assert calls == [("repoint", root / "run_current.cmd")]
    sw.rollback()
    assert calls[-1] == ("restore",) and ReleaseSet(root).current() is None


def test_first_move_with_open_trades_is_green_and_points_the_tick_task_at_the_layout(tmp_path):
    w, sw, calls, root, live = first_move(
        tmp_path, positions={"MSFT": held()}, orders=[stop_order()], rows={"MSFT": row()}
    )
    res = go(sw, w.env(), hot=True, target="main")
    assert res.status == "done", res.lines
    assert calls == [("repoint", root / "run_current.cmd")]
    assert rules_version_of(w.journal, "MSFT", date(2026, 10, 14)) == "r1"


def test_first_move_goes_back_to_the_old_folder_if_the_next_tick_fails(tmp_path):
    w, sw, calls, root, live = first_move(
        tmp_path, positions={"MSFT": held()}, orders=[stop_order()], rows={"MSFT": row()},
        after_tick=TickInfo(1, datetime(2026, 10, 15, 12, 0, tzinfo=ET), 0),
    )  # fmt: skip
    res = go(sw, w.env(), hot=True, target="main")
    assert res.status == "rolled_back"
    assert calls[-1] == ("restore",) and ReleaseSet(root).current() is None
    assert w.messages[-1][0] == "CRITICAL"


def test_first_move_is_refused_if_a_stop_is_missing(tmp_path):
    w, sw, calls, root, live = first_move(
        tmp_path, positions={"MSFT": held()}, orders=[], rows={"MSFT": row()}
    )
    assert go(sw, w.env(), hot=True, target="main").status == "blocked" and calls == []


def test_a_tick_that_is_still_running_does_not_count_as_the_next_tick():
    started = TickInfo(267009, datetime(2026, 10, 15, 12, 0, tzinfo=ET), 0)
    assert not new_tick_finished(BEFORE, started)  # "last run" moved, but it has not finished
    assert not new_tick_finished(BEFORE, BEFORE)  # nothing new yet
    assert new_tick_finished(BEFORE, AFTER)
