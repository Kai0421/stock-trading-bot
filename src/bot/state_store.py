# Author: Kai0421
"""Persist ``data/bot.db`` between GitHub Actions runs as a SQL text dump.

Each Actions run starts on a fresh machine. The engine workflow restores the database from
``bot.sql`` on the ``bot-state`` branch, runs the job, then dumps it back and commits.
A text dump keeps git history small (it delta-compresses) and is human-readable.

CLI: ``python -m bot.state_store restore --sql state/bot.sql --db data/bot.db``
     ``python -m bot.state_store dump --db data/bot.db --sql state/bot.sql``
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from pathlib import Path


def dump(db_path: Path, sql_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        text = "\n".join(conn.iterdump()) + "\n"
    finally:
        conn.close()
    sql_path.parent.mkdir(parents=True, exist_ok=True)
    sql_path.write_text(text)


def restore(sql_path: Path, db_path: Path) -> None:
    """Rebuild ``db_path`` from the dump, replacing any existing file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(sql_path.read_text())
        conn.commit()
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.state_store")
    ap.add_argument("action", choices=["dump", "restore"])
    ap.add_argument("--db", type=Path, default=Path("data/bot.db"))
    ap.add_argument("--sql", type=Path, default=Path("state/bot.sql"))
    args = ap.parse_args(argv)
    if args.action == "restore":
        if not args.sql.exists():
            print(f"nothing to restore ({args.sql} missing): starting with an empty database")
            return 0
        restore(args.sql, args.db)
        print(f"restored {args.db} from {args.sql}")
        return 0
    if not args.db.exists():
        print(f"nothing to dump ({args.db} missing)")
        return 0
    dump(args.db, args.sql)
    print(f"dumped {args.db} to {args.sql}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
