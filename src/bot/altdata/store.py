# Author: Kai0421
"""Point-in-time cache for alternative data, in the bot's SQLite DB (tables insider_txns,
filings, news). Every row carries its filing/publication time, never the trade date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from bot.journal import Journal

SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_txns (
    accession TEXT NOT NULL, idx INTEGER NOT NULL, symbol TEXT NOT NULL, owner TEXT NOT NULL,
    is_officer INTEGER NOT NULL, is_director INTEGER NOT NULL, title TEXT, code TEXT NOT NULL,
    shares REAL, price REAL, tx_date TEXT, filed TEXT NOT NULL, plan_10b5_1 INTEGER NOT NULL,
    PRIMARY KEY (accession, idx)
);
CREATE INDEX IF NOT EXISTS insider_by_symbol ON insider_txns (symbol, filed);
CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY, symbol TEXT NOT NULL, form TEXT NOT NULL, filed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, created_at TEXT NOT NULL, headline TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS news_by_symbol ON news (symbol, created_at);
"""


@dataclass(frozen=True)
class InsiderTxn:
    accession: str
    idx: int
    symbol: str
    owner: str
    is_officer: bool
    is_director: bool
    title: str
    code: str  # Form 4 transaction code: P open-market purchase, S open-market sale, ...
    shares: float
    price: float
    tx_date: date | None
    filed: date  # filing date: the point-in-time key
    plan_10b5_1: bool


class AltStore:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.db = journal.db
        self.db.executescript(SCHEMA)
        self.db.commit()

    def add_txns(self, txns: list[InsiderTxn]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO insider_txns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (t.accession, t.idx, t.symbol, t.owner, int(t.is_officer), int(t.is_director),
                 t.title, t.code, t.shares, t.price, t.tx_date.isoformat() if t.tx_date else None,
                 t.filed.isoformat(), int(t.plan_10b5_1))
                for t in txns
            ],
        )  # fmt: skip
        self.db.commit()

    def add_filing(self, accession: str, symbol: str, form: str, filed: date) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO filings VALUES (?,?,?,?)",
            (accession, symbol, form, filed.isoformat()),
        )
        self.db.commit()

    def has_filing(self, accession: str) -> bool:
        row = self.db.execute("SELECT 1 FROM filings WHERE accession = ?", (accession,))
        return row.fetchone() is not None

    def add_news(self, news_id: int, symbol: str, created_at: datetime, headline: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO news VALUES (?,?,?,?)",
            (int(news_id), symbol, created_at.isoformat(), headline),
        )
        self.db.commit()

    def txns(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        """Transactions filed in [start, end)."""
        rows = self.db.execute(
            "SELECT * FROM insider_txns WHERE symbol = ? AND filed >= ? AND filed < ?",
            (symbol, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    def filings(self, symbol: str, forms: set[str], start: date, end: date) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM filings WHERE symbol = ? AND filed >= ? AND filed < ?",
            (symbol, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows if r["form"] in forms]

    def news(self, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM news WHERE symbol = ?", (symbol,)).fetchall()
        out = []
        for r in rows:
            created = datetime.fromisoformat(r["created_at"])
            if start <= created < end:
                out.append(dict(r))
        return out
