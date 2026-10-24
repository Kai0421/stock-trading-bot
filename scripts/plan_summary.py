# Author: Kai0421
"""Post today's plan to Slack: what to buy and what to sell (reads the journal, writes nothing).

    uv run python scripts/plan_summary.py --print     # show it
    uv run python scripts/plan_summary.py             # post to the trading-bot channel

Run from the lab folder (Slack settings live in its .env). The live journal is opened read-only.
Wording only: it decides and orders nothing.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from bot import notify
from bot.data import ET
from bot.plan_summary import summary

LIVE_DIR = Path(__file__).resolve().parents[2] / "StockTradingBot"


def read(db: Path, day: str):
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        ran = con.execute(
            "SELECT 1 FROM runs WHERE job = 'premarket' AND status = 'ok' AND started_at LIKE ?",
            (f"{day}%",),
        ).fetchone()
        plans = [
            SimpleNamespace(**dict(r))
            for r in con.execute(
                "SELECT symbol, win_prob FROM plans WHERE day = ? ORDER BY rank", (day,)
            )
        ]
        signals = [
            dict(r) for r in con.execute("SELECT * FROM signals WHERE day = ? ORDER BY id", (day,))
        ]
        positions = []
        for r in con.execute("SELECT * FROM positions ORDER BY symbol"):
            d = dict(r)
            pending = d["pending_exit"]
            if pending:
                import json

                kind, qty = json.loads(pending) if isinstance(pending, str) else pending
                pending = (kind, qty)
            positions.append(
                SimpleNamespace(
                    symbol=d["symbol"],
                    shares=d["shares"],
                    stop=d["stop"],
                    days_held=d["days_held"],
                    pending_exit=pending or None,
                )
            )
        return bool(ran), plans, signals, positions
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true", help="print only, no Slack")
    ap.add_argument("--live", type=Path, default=LIVE_DIR)
    args = ap.parse_args(argv)

    now = datetime.now(ET)
    ran, plans, signals, positions = read(args.live / "data" / "bot.db", now.date().isoformat())
    title, text = summary(f"{now:%a %d %b}", ran, plans, signals, positions)
    print(f"{title}\n{text}")
    if args.show:
        return 0
    return notify.main(["--category", "bot", "--level", "INFO", "--title", title, text])


if __name__ == "__main__":
    sys.exit(main())
