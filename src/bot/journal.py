# Author: Kai0421
"""SQLite bot state (``data/bot.db``): the trading code's source of truth after Alpaca.

Tables follow docs/05-memory.md (runs, signals, orders, positions, trades, equity, flags)
plus helpers the engine needs: plans (tomorrow's ranked entries), unsettled sale
proceeds (T+1), alert de-duplication, completed schedule slots and a small key/value
store. Agents never write here. All timestamps are stored as ISO-8601 strings.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, job TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL DEFAULT 'running', error TEXT
);
CREATE TABLE IF NOT EXISTS slots (job TEXT NOT NULL, slot TEXT NOT NULL, PRIMARY KEY (job, slot));
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, symbol TEXT NOT NULL, signature TEXT,
    win_prob REAL, lower_bound REAL, sample_size INTEGER, taken INTEGER NOT NULL,
    reason TEXT NOT NULL, features TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    day TEXT NOT NULL, rank INTEGER NOT NULL, symbol TEXT NOT NULL, signature TEXT,
    win_prob REAL, atr REAL, momentum REAL, sector TEXT, last_close REAL,
    PRIMARY KEY (day, symbol)
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY, alpaca_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, limit_price REAL,
    stop_price REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, entry_price REAL NOT NULL, shares INTEGER NOT NULL,
    shares_initial INTEGER NOT NULL, initial_stop REAL NOT NULL, stop REAL NOT NULL,
    entry_date TEXT NOT NULL, days_held INTEGER NOT NULL, partial_taken INTEGER NOT NULL,
    signature TEXT, win_prob REAL, sector TEXT, stop_order_id TEXT, pending_exit TEXT,
    realized_proceeds REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, entry_date TEXT NOT NULL,
    exit_date TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL,
    shares INTEGER NOT NULL, pnl REAL NOT NULL, r_multiple REAL NOT NULL,
    exit_reason TEXT NOT NULL, holding_days INTEGER NOT NULL, signature TEXT, win_prob REAL
);
CREATE TABLE IF NOT EXISTS equity (day TEXT PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL);
CREATE TABLE IF NOT EXISTS flags (
    name TEXT PRIMARY KEY, value INTEGER NOT NULL, reason TEXT, set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unsettled (id INTEGER PRIMARY KEY, amount REAL NOT NULL, settles_on TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, last_sent TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""  # noqa: E501


@dataclass(frozen=True)
class Flag:
    value: bool
    reason: str
    set_at: str


@dataclass(frozen=True)
class PositionRow:
    """The bot's management state for one open position."""

    symbol: str
    entry_price: float
    shares: int
    shares_initial: int
    initial_stop: float
    stop: float
    entry_date: date
    days_held: int
    partial_taken: bool
    signature: str
    win_prob: float | None
    sector: str
    stop_order_id: str | None
    pending_exit: tuple[str, int] | None  # (kind, shares) decided at EOD
    realized_proceeds: float = 0.0


@dataclass(frozen=True)
class PlanRow:
    """A ranked entry planned by ``premarket`` for ``day``."""

    day: date
    rank: int
    symbol: str
    signature: str
    win_prob: float
    atr: float
    momentum: float
    sector: str
    last_close: float


class Journal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cur = self.db.execute(sql, params)
        self.db.commit()
        return cur

    # ----------------------------------------------------------------- runs

    def start_run(self, job: str, now: datetime) -> int:
        cur = self._exec("INSERT INTO runs (job, started_at) VALUES (?, ?)", (job, now.isoformat()))
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, now: datetime, error: str | None = None) -> None:
        self._exec(
            "UPDATE runs SET finished_at = ?, status = ?, error = ? WHERE id = ?",
            (now.isoformat(), status, error, run_id),
        )

    def last_runs(self) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT job, status FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY job)"
        ).fetchall()
        return {r["job"]: r["status"] for r in rows}

    def jobs_completed_on(self, day: date) -> set[str]:
        rows = self.db.execute(
            "SELECT DISTINCT job FROM runs WHERE status = 'ok' AND substr(started_at, 1, 10) = ?",
            (day.isoformat(),),
        ).fetchall()
        return {r["job"] for r in rows}

    def slot_done(self, job: str, slot: str) -> bool:
        row = self.db.execute("SELECT 1 FROM slots WHERE job = ? AND slot = ?", (job, slot))
        return row.fetchone() is not None

    def mark_slot(self, job: str, slot: str) -> None:
        self._exec("INSERT OR IGNORE INTO slots (job, slot) VALUES (?, ?)", (job, slot))

    # ----------------------------------------------------------------- flags

    def set_flag(self, name: str, value: bool, reason: str, now: datetime) -> None:
        self._exec(
            "INSERT OR REPLACE INTO flags (name, value, reason, set_at) VALUES (?, ?, ?, ?)",
            (name, int(value), reason, now.isoformat()),
        )

    def flags(self) -> dict[str, Flag]:
        rows = self.db.execute("SELECT * FROM flags").fetchall()
        return {r["name"]: Flag(bool(r["value"]), r["reason"] or "", r["set_at"]) for r in rows}

    def flag(self, name: str) -> bool:
        f = self.flags().get(name)
        return bool(f and f.value)

    # ----------------------------------------------------------------- positions / plans

    def upsert_position(self, p: PositionRow) -> None:
        self._exec(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.symbol, p.entry_price, p.shares, p.shares_initial, p.initial_stop, p.stop,
                p.entry_date.isoformat(), p.days_held, int(p.partial_taken), p.signature,
                p.win_prob, p.sector, p.stop_order_id,
                json.dumps(list(p.pending_exit)) if p.pending_exit else None,
                p.realized_proceeds,
            ),
        )  # fmt: skip

    def positions(self) -> dict[str, PositionRow]:
        out: dict[str, PositionRow] = {}
        for r in self.db.execute("SELECT * FROM positions ORDER BY symbol").fetchall():
            pending = json.loads(r["pending_exit"]) if r["pending_exit"] else None
            out[r["symbol"]] = PositionRow(
                symbol=r["symbol"],
                entry_price=r["entry_price"],
                shares=r["shares"],
                shares_initial=r["shares_initial"],
                initial_stop=r["initial_stop"],
                stop=r["stop"],
                entry_date=date.fromisoformat(r["entry_date"]),
                days_held=r["days_held"],
                partial_taken=bool(r["partial_taken"]),
                signature=r["signature"] or "",
                win_prob=r["win_prob"],
                sector=r["sector"] or "",
                stop_order_id=r["stop_order_id"],
                pending_exit=(pending[0], int(pending[1])) if pending else None,
                realized_proceeds=r["realized_proceeds"],
            )
        return out

    def delete_position(self, symbol: str) -> None:
        self._exec("DELETE FROM positions WHERE symbol = ?", (symbol,))

    def save_plans(self, day: date, plans: list[PlanRow]) -> None:
        self.db.execute("DELETE FROM plans WHERE day = ?", (day.isoformat(),))
        self.db.executemany(
            "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (day.isoformat(), p.rank, p.symbol, p.signature, p.win_prob, p.atr, p.momentum,
                 p.sector, p.last_close)
                for p in plans
            ],
        )  # fmt: skip
        self.db.commit()

    def plans(self, day: date) -> list[PlanRow]:
        rows = self.db.execute(
            "SELECT * FROM plans WHERE day = ? ORDER BY rank", (day.isoformat(),)
        ).fetchall()
        return [
            PlanRow(
                day=date.fromisoformat(r["day"]),
                rank=r["rank"],
                symbol=r["symbol"],
                signature=r["signature"],
                win_prob=r["win_prob"],
                atr=r["atr"],
                momentum=r["momentum"],
                sector=r["sector"],
                last_close=r["last_close"],
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- signals / orders

    def record_signal(
        self,
        day: date,
        symbol: str,
        signature: str | None,
        win_prob: float | None,
        lower_bound: float | None,
        sample_size: int,
        taken: bool,
        reason: str,
        features: dict[str, Any],
    ) -> None:
        self._exec(
            "INSERT INTO signals (day, symbol, signature, win_prob, lower_bound, sample_size,"
            " taken, reason, features) VALUES (?,?,?,?,?,?,?,?,?)",
            (day.isoformat(), symbol, signature, win_prob, lower_bound, sample_size, int(taken),
             reason, json.dumps(features, default=str)),
        )  # fmt: skip

    def signals(self, start: date, end: date) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM signals WHERE day BETWEEN ? AND ? ORDER BY id",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    def rejection_counts(self, start: date, end: date) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT reason, COUNT(*) AS n FROM signals WHERE taken = 0 AND day BETWEEN ? AND ?"
            " GROUP BY reason ORDER BY n DESC, reason",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return {r["reason"]: r["n"] for r in rows}

    def record_order(
        self,
        client_order_id: str,
        alpaca_id: str | None,
        symbol: str,
        side: str,
        qty: float,
        type_: str,
        status: str,
        now: datetime,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_order_id) DO"
            " UPDATE SET alpaca_id = COALESCE(excluded.alpaca_id, alpaca_id),"
            " status = excluded.status, updated_at = excluded.updated_at",
            (client_order_id, alpaca_id, symbol, side, qty, type_, status, limit_price,
             stop_price, now.isoformat(), now.isoformat()),
        )  # fmt: skip

    def orders_on(self, day: date) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM orders WHERE substr(created_at, 1, 10) = ? ORDER BY created_at",
            (day.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_order(self, symbol: str, side: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM orders WHERE symbol = ? AND side = ? ORDER BY created_at DESC LIMIT 1",
            (symbol, side),
        ).fetchone()
        return dict(row) if row else None

    def has_order(self, client_order_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM orders WHERE client_order_id = ?", (client_order_id,))
        return row.fetchone() is not None

    # ----------------------------------------------------------------- trades

    def record_trade(
        self,
        symbol: str,
        entry_date: date,
        exit_date: date,
        entry_price: float,
        exit_price: float,
        shares: int,
        pnl: float,
        r_multiple: float,
        exit_reason: str,
        signature: str,
        win_prob: float | None,
    ) -> None:
        self._exec(
            "INSERT INTO trades (symbol, entry_date, exit_date, entry_price, exit_price, shares,"
            " pnl, r_multiple, exit_reason, holding_days, signature, win_prob)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, entry_date.isoformat(), exit_date.isoformat(), entry_price, exit_price,
             shares, pnl, r_multiple, exit_reason, (exit_date - entry_date).days, signature,
             win_prob),
        )  # fmt: skip

    def trades(self, since: date | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM trades", ()
        if since is not None:
            sql, params = sql + " WHERE exit_date >= ?", (since.isoformat(),)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY exit_date, id", params)]

    def outcomes_by_signature(self) -> dict[str, list[bool]]:
        out: dict[str, list[bool]] = defaultdict(list)
        for t in self.trades():
            out[t["signature"]].append(t["pnl"] > 0)
        return dict(out)

    # ----------------------------------------------------------------- equity / cash

    def record_equity(self, day: date, equity: float, cash: float) -> None:
        self._exec(
            "INSERT OR REPLACE INTO equity VALUES (?, ?, ?)", (day.isoformat(), equity, cash)
        )

    def equity_history(self) -> pd.DataFrame:
        rows = self.db.execute("SELECT * FROM equity ORDER BY day").fetchall()
        df = pd.DataFrame([dict(r) for r in rows], columns=["day", "equity", "cash"]).astype(
            {"equity": "float64", "cash": "float64"}
        )
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("day")), name=None)
        df["peak"] = df["equity"].cummax()
        df["drawdown"] = 1 - df["equity"] / df["peak"]
        return df

    def peak_equity(self) -> float | None:
        row = self.db.execute("SELECT MAX(equity) AS m FROM equity").fetchone()
        return row["m"]

    def previous_equity(self, day: date) -> float | None:
        row = self.db.execute(
            "SELECT equity FROM equity WHERE day < ? ORDER BY day DESC LIMIT 1", (day.isoformat(),)
        ).fetchone()
        return row["equity"] if row else None

    def add_unsettled(self, amount: float, settles_on: date) -> None:
        self._exec(
            "INSERT INTO unsettled (amount, settles_on) VALUES (?, ?)",
            (amount, settles_on.isoformat()),
        )

    def unsettled_total(self, today: date) -> float:
        """Sale proceeds not yet settled as of the start of ``today``."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM unsettled WHERE settles_on > ?",
            (today.isoformat(),),
        ).fetchone()
        return float(row["s"])

    # ----------------------------------------------------------------- alerts / kv

    def should_send(self, key: str, now: datetime, window: timedelta) -> bool:
        """True (and records it) unless the same alert went out within ``window``."""
        row = self.db.execute("SELECT last_sent FROM alerts WHERE key = ?", (key,)).fetchone()
        if row and now - datetime.fromisoformat(row["last_sent"]) < window:
            return False
        self._exec("INSERT OR REPLACE INTO alerts VALUES (?, ?)", (key, now.isoformat()))
        return True

    def once(self, key: str) -> bool:
        """True the first time ``key`` is seen, False afterwards."""
        cur = self._exec("INSERT OR IGNORE INTO kv (key, value) VALUES (?, '1')", (f"once:{key}",))
        return cur.rowcount == 1

    def get_kv(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
