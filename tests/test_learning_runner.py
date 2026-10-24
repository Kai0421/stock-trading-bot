# Author: Kai0421
"""The learning-loop runner reads the journal read-only and writes only its own report files."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pandas as pd

from bot.journal import SCHEMA

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research" / "learning_loop.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("learning_loop", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_journal(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute(
        "INSERT INTO trades (symbol, entry_date, exit_date, entry_price, exit_price, shares, pnl,"
        " r_multiple, exit_reason, holding_days, signature, win_prob)"
        " VALUES ('MDT','2026-10-14','2026-10-15',92.81,97.64,207,449.19,0.4657,"
        "'profit_target',1,'sig',0.7)"
    )
    con.execute(
        "INSERT INTO signals (day, symbol, signature, taken, reason, features)"
        " VALUES ('2026-10-14','MDT','sig',1,'planned','{\"close\": 93.01, \"atr\": 2.4}')"
    )
    con.commit()
    con.close()


def test_runner_writes_reports_and_leaves_the_journal_alone(tmp_path, capsys):
    db = tmp_path / "bot.db"
    make_journal(db)
    before = db.read_bytes()
    runner = load_runner()
    out = tmp_path / "out"
    rc = runner.main(
        ["--print", "--journal", str(db), "--council", str(tmp_path / "none.csv"),
         "--bars", str(tmp_path / "no-bars"), "--out", str(out)]
    )  # fmt: skip
    assert rc == 0
    assert db.read_bytes() == before  # the journal is byte-for-byte unchanged
    pm = pd.read_csv(out / "postmortems.csv")
    assert list(pm["symbol"]) == ["MDT"]
    assert "profit target" in pm.iloc[0]["lesson"]
    assert (out / "learning_report.md").exists()
    assert "Nothing to change" in capsys.readouterr().out


def test_journal_is_opened_read_only(tmp_path):
    db = tmp_path / "bot.db"
    make_journal(db)
    trades, signals = load_runner().read_journal(db)
    assert len(trades) == 1 and len(signals) == 1
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        con.execute("DELETE FROM trades")
        raise AssertionError("a read-only connection must not allow writes")
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
