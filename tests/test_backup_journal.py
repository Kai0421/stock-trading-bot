# Author: Kai0421
import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "backup_journal", Path(__file__).parents[1] / "scripts" / "backup_journal.py"
)
bj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bj)


def test_backup_copies_rows_and_prunes(tmp_path):
    db = tmp_path / "bot.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    dest = tmp_path / "bk"
    for day in range(1, 5):
        out = bj.backup(db, dest, keep=2, now=datetime(2026, 10, day, 9, 0))
    assert len(list(dest.glob("bot-*.db"))) == 2
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchone() == (1,)
