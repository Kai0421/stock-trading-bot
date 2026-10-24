# Author: Kai0421
"""The notifier's own small state: which messages were sent recently, and stored thread ids.

A separate SQLite file, so the notifier never reads or writes the trading journal.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


class State:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def should_send(self, key: str, now: datetime, window: timedelta) -> bool:
        """False when the same message went out within ``window``; records this send otherwise."""
        row = self.db.execute("SELECT at FROM sent WHERE key = ?", (key,)).fetchone()
        if row and now - datetime.fromisoformat(row[0]) < window:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO sent (key, at) VALUES (?, ?)", (key, now.isoformat())
        )
        self.db.commit()
        return True

    def get_kv(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
