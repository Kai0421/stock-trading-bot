# Author: Kai0421
"""Tests for bot.state_store: bot.db <-> SQL text dump (persisted between Actions runs)."""

from datetime import UTC, date, datetime

from bot.journal import Journal
from bot.state_store import dump, main, restore


def test_dump_restore_roundtrip(tmp_path):
    j = Journal(tmp_path / "a.db")
    j.set_flag("paused", True, "dd", datetime(2026, 10, 5, tzinfo=UTC))
    j.record_equity(date(2026, 10, 5), 100_500.0, 50_000.0)
    j.set_kv("slack_thread:weekly", "123.4")
    j.db.close()
    sql = tmp_path / "state" / "bot.sql"
    dump(tmp_path / "a.db", sql)
    assert sql.read_text().startswith("BEGIN TRANSACTION;")
    restore(sql, tmp_path / "b" / "bot.db")
    j2 = Journal(tmp_path / "b" / "bot.db")
    assert j2.flag("paused")
    assert j2.peak_equity() == 100_500.0
    assert j2.get_kv("slack_thread:weekly") == "123.4"


def test_restore_replaces_existing_db(tmp_path):
    Journal(tmp_path / "a.db").set_kv("x", "old")
    dump(tmp_path / "a.db", tmp_path / "s.sql")
    target = tmp_path / "t.db"
    stale = Journal(target)
    stale.set_kv("x", "stale")
    stale.db.close()  # Windows can't delete a file that is still open
    restore(tmp_path / "s.sql", target)
    assert Journal(target).get_kv("x") == "old"


def test_cli_skips_missing_inputs(tmp_path, capsys):
    assert (
        main(["restore", "--sql", str(tmp_path / "none.sql"), "--db", str(tmp_path / "x.db")]) == 0
    )
    assert not (tmp_path / "x.db").exists()
    assert main(["dump", "--db", str(tmp_path / "none.db"), "--sql", str(tmp_path / "o.sql")]) == 0
    assert "nothing to" in capsys.readouterr().out
