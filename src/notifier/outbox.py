# Author: Kai0421
"""A queue between the trading bot and Slack.

The bot only writes a message into a small SQLite file (fast, local, cannot hang on the network).
A separate scheduled task (``python -m notifier drain``) reads the queue and sends, so Slack being
slow, or the notifier being changed or broken, cannot delay a trading job. A critical message that
nobody has sent after a few minutes is sent directly by the next bot job (``rescue``), so a stopped
drain task cannot silence a safety alert. Off unless NOTIFY_VIA_OUTBOX is set.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from notifier.core import Delivery, Level

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


class Outbox:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, category TEXT NOT NULL, "
            "level TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, thread TEXT, "
            "status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT, sent_at TEXT)"
        )
        self.db.commit()

    def enqueue(
        self,
        level: Level,
        title: str,
        text: str,
        category: str | None,
        thread: str | None,
        now: datetime,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO messages (created, category, level, title, text, thread) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(), category or "bot", level.value, title, text, thread),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def pending(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE status = 'queued' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def mark_sent(self, id_: int, now: datetime) -> None:
        self.db.execute(
            "UPDATE messages SET status = 'sent', sent_at = ? WHERE id = ?", (now.isoformat(), id_)
        )
        self.db.commit()

    def mark_attempt(self, id_: int, error: str, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.db.execute(
            "UPDATE messages SET attempts = attempts + 1, last_error = ?, "
            "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END WHERE id = ?",
            (error, max_attempts, id_),
        )
        self.db.commit()

    def failed_count(self) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM messages WHERE status = 'failed'").fetchone()
        return int(row[0])

    def overdue_critical(self, now: datetime, after: timedelta) -> list[sqlite3.Row]:
        cutoff = (now - after).isoformat()
        return self.db.execute(
            "SELECT * FROM messages WHERE status = 'queued' AND level = 'CRITICAL' "
            "AND created <= ? ORDER BY id",
            (cutoff,),
        ).fetchall()


def drain(
    outbox: Outbox, dispatcher: Any, now: datetime, max_attempts: int = MAX_ATTEMPTS
) -> tuple[int, int]:
    """Send everything queued. Returns (sent, failed this round)."""
    sent = failed = 0
    for row in outbox.pending():
        result = dispatcher.notify(
            Level(row["level"]),
            row["title"],
            row["text"],
            thread=row["thread"],
            category=row["category"],
        )
        if result.ok:
            outbox.mark_sent(row["id"], now)
            sent += 1
        else:
            outbox.mark_attempt(row["id"], "not delivered", max_attempts)
            failed += 1
    return sent, failed


class OutboxDispatcher:
    """Drop-in for the bot's dispatcher: ``notify`` queues the message and returns at once."""

    def __init__(
        self,
        outbox: Any,
        direct: Any,
        clock: Callable[[], datetime] = datetime.now,
        rescue_after: timedelta = timedelta(minutes=3),
    ) -> None:
        self.outbox = outbox
        self.direct = direct
        self.clock = clock
        self.rescue_after = rescue_after

    def notify(
        self,
        level: Level,
        title: str,
        text: str = "",
        files: Sequence[Path] = (),
        thread: str | None = None,
        category: str | None = None,
    ) -> Delivery:
        if files:  # attachments cannot wait in a text queue
            return self.direct.notify(level, title, text, files, thread, category)
        try:
            self.outbox.enqueue(level, title, text, category, thread, self.clock())
            return Delivery(True, None)
        except Exception as exc:  # a broken queue must never lose a message
            log.error("outbox unavailable (%s); sending directly", exc)
            return self.direct.notify(level, title, text, files, thread, category)

    def rescue(self) -> None:
        """Send directly any critical message the drain task has not sent after a few minutes."""
        try:
            for row in self.outbox.overdue_critical(self.clock(), self.rescue_after):
                result = self.direct.notify(
                    Level.CRITICAL,
                    row["title"],
                    row["text"],
                    thread=row["thread"],
                    category=row["category"],
                )
                if result.ok:
                    self.outbox.mark_sent(row["id"], self.clock())
        except Exception as exc:
            log.error("outbox rescue failed: %s", exc)
