# Author: Kai0421
"""Weekly safe copy of the journal (SQLite online backup, read-only on the source).

uv run python scripts/backup_journal.py --db data/bot.db --to <folder> [--keep 8]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup(db: Path, dest: Path, keep: int = 8, now: datetime | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"bot-{(now or datetime.now(UTC)):%Y-%m-%d-%H%M}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        out.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed the integrity check: {ok}")
    for old in sorted(dest.glob("bot-*.db"))[:-keep]:
        old.unlink()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/bot.db"))
    ap.add_argument("--to", type=Path, required=True)
    ap.add_argument("--keep", type=int, default=8)
    a = ap.parse_args()
    print(f"journal backed up to {backup(a.db, a.to, a.keep)}")


if __name__ == "__main__":
    main()
