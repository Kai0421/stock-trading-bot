# Author: Kai0421
"""Hot-upgrade building blocks: switch window, release pointer, tick lock, version stamps."""

import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot.journal import Journal, PositionRow
from bot.release import (
    BASELINE_VERSION,
    ReleaseError,
    ReleaseSet,
    SwitchLock,
    TickRunning,
    env_matches,
    freeze_open_positions,
    link_dir,
    record_switch,
    rules_version_of,
    sync_env,
    window_blocker,
)

ET = ZoneInfo("America/New_York")


def et(h, m=0, day=15):
    return datetime(2026, 10, day, h, m, tzinfo=ET)


OPEN, CLOSE = et(9, 30), et(16, 0)


def position_row(symbol, entry_date=date(2026, 10, 14)):
    return PositionRow(
        symbol,
        100.0,
        10,
        10,
        96.0,
        96.0,
        entry_date,
        1,
        False,
        "sig",
        0.7,
        "Information Technology",
        None,
        None,
    )


# ------------------------------------------------------------------ switch window


@pytest.mark.parametrize(
    "now, blocked",
    [
        (et(8, 45), True),  # stop swap / pre-market
        (et(9, 31), True),  # the open
        (et(9, 59), True),  # still placing entries
        (et(10, 0), False),
        (et(12, 0), False),
        (et(15, 30), False),
        (et(15, 31), True),  # end-of-day jobs and the overnight buy are close
        (et(16, 5), True),
    ],
)
def test_window_blocker_keeps_clear_of_open_and_close(now, blocked):
    assert (window_blocker(now, OPEN, CLOSE) is not None) is blocked


def test_window_blocker_closed_day_and_early_close():
    assert "closed" in window_blocker(et(12), None, None)
    assert window_blocker(et(12, 40), OPEN, et(13, 0)) is not None  # early close at 13:00
    assert window_blocker(et(11, 0), OPEN, et(13, 0)) is None


def test_window_blocker_reasons_are_plain_english():
    why = window_blocker(et(9, 40), OPEN, CLOSE)
    assert why and "_" not in why and "open" in why


# ------------------------------------------------------------------ release pointer


def test_pointer_switch_and_rollback(tmp_path):
    rs = ReleaseSet(tmp_path)
    a, b = tmp_path / "releases" / "a", tmp_path / "releases" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert rs.current() is None
    rs.point_to(a, now=et(12))
    assert rs.current() == a and rs.previous() is None
    rs.point_to(b, now=et(12, 5))
    assert rs.current() == b and rs.previous() == a
    assert rs.rollback() == a
    assert rs.current() == a and rs.previous() == b  # roll forward is possible too
    log = [json.loads(x) for x in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert [e["to"] for e in log] == [str(a), str(b), str(a)]


def test_pointer_write_is_atomic_no_temp_left(tmp_path):
    rs = ReleaseSet(tmp_path)
    a = tmp_path / "releases" / "a"
    a.mkdir(parents=True)
    rs.point_to(a, now=et(12))
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == [
        "current.txt",
        "history.jsonl",
    ]


def test_rollback_without_previous_refuses(tmp_path):
    rs = ReleaseSet(tmp_path)
    with pytest.raises(ReleaseError):
        rs.rollback()


def test_point_to_missing_folder_refuses(tmp_path):
    with pytest.raises(ReleaseError):
        ReleaseSet(tmp_path).point_to(tmp_path / "releases" / "nope", now=et(12))


# ------------------------------------------------------------------ tick lock


def test_switch_lock_refuses_while_a_tick_runs_and_releases_after(tmp_path):
    path = tmp_path / "data" / "engine.lock"
    with SwitchLock(path), pytest.raises(TickRunning), SwitchLock(path):
        pass
    with SwitchLock(path):  # free again
        pass


def test_a_running_tick_skips_while_the_switch_holds_the_lock(tmp_path):
    """The engine's own lock is the one the switch takes, so a tick during a switch skips."""
    from bot.filelock import try_lock

    path = tmp_path / "engine.lock"
    with SwitchLock(path), open(path, "w") as other:
        assert try_lock(other) is False


# ------------------------------------------------------------------ rules version stamps


def test_unstamped_positions_are_baseline_until_a_switch_is_recorded(tmp_path):
    j = Journal(tmp_path / "bot.db")
    assert rules_version_of(j, "AAPL", date(2026, 10, 14)) == BASELINE_VERSION


def test_freeze_stamps_open_positions_with_the_outgoing_version(tmp_path):
    j = Journal(tmp_path / "bot.db")
    j.upsert_position(position_row("AAPL", entry_date=date(2026, 10, 14)))
    j.upsert_position(position_row("MSFT", entry_date=date(2026, 10, 14)))
    assert sorted(freeze_open_positions(j, "r1")) == ["AAPL", "MSFT"]
    record_switch(j, "r2", et(12))
    # new entry after the switch gets the new version, old positions keep theirs
    assert rules_version_of(j, "AAPL", date(2026, 10, 14)) == "r1"
    assert rules_version_of(j, "NVDA", date(2026, 10, 16)) == "r2"
    assert freeze_open_positions(j, "r2") == []  # AAPL already stamped; nothing else is held


def test_freeze_is_idempotent_and_never_restamps(tmp_path):
    j = Journal(tmp_path / "bot.db")
    j.upsert_position(position_row("AAPL", entry_date=date(2026, 10, 14)))
    freeze_open_positions(j, "r1")
    assert freeze_open_positions(j, "r2") == []  # already stamped with r1
    assert rules_version_of(j, "AAPL", date(2026, 10, 14)) == "r1"


def test_a_stale_stamp_never_labels_a_later_trade_in_the_same_stock(tmp_path):
    j = Journal(tmp_path / "bot.db")
    j.upsert_position(position_row("AAPL", entry_date=date(2026, 10, 14)))
    freeze_open_positions(j, "r1")
    record_switch(j, "r2", et(12))
    # AAPL closed, then bought again on the 20th: the old r1 stamp must not apply
    assert rules_version_of(j, "AAPL", date(2026, 10, 20)) == "r2"


def test_stamps_do_not_touch_existing_tables(tmp_path):
    """Additive only: the stamps live in the kv table, so old code reads the DB unchanged."""
    j = Journal(tmp_path / "bot.db")
    before = {r[0]: r[1] for r in j.db.execute("select name, sql from sqlite_master")}
    j.upsert_position(position_row("AAPL"))
    freeze_open_positions(j, "r1")
    record_switch(j, "r2", et(12))
    after = {r[0]: r[1] for r in j.db.execute("select name, sql from sqlite_master")}
    assert before == after


# ------------------------------------------------------------------ shared state links


def test_link_dir_shares_writes(tmp_path):
    shared = tmp_path / "shared" / "data"
    shared.mkdir(parents=True)
    release = tmp_path / "releases" / "a"
    release.mkdir(parents=True)
    link_dir(release / "data", shared)
    (release / "data" / "bot.db").write_text("x")
    assert (shared / "bot.db").read_text() == "x"


def test_link_dir_replaces_a_tracked_folder_but_keeps_its_files_out_of_shared(tmp_path):
    shared = tmp_path / "shared" / "memory"
    shared.mkdir(parents=True)
    release = tmp_path / "releases" / "a"
    (release / "memory").mkdir(parents=True)
    (release / "memory" / "README.md").write_text("tracked")
    link_dir(release / "memory", shared)
    assert not (shared / "README.md").exists()  # never copied into shared state


def test_sync_env_copies_and_verifies(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / ".env").write_text("TRADING_MODE=swing\n")
    rel = tmp_path / "rel"
    rel.mkdir()
    sync_env(rel, shared)
    assert env_matches(rel, shared)
    (rel / ".env").write_text("TRADING_MODE=overnight\n")
    assert not env_matches(rel, shared)


def test_sync_env_missing_shared_env_refuses(tmp_path):
    (tmp_path / "rel").mkdir()
    with pytest.raises(ReleaseError):
        sync_env(tmp_path / "rel", tmp_path / "shared")


def test_module_is_not_imported_by_the_order_path():
    src = Path(__file__).parents[1] / "src" / "bot"
    for name in ("engine.py", "decide.py", "execute.py", "gather.py", "config.py"):
        text = (src / name).read_text()
        assert "bot.release" not in text and "bot.shadowtick" not in text, name
    assert os.path.exists(src / "release.py")
