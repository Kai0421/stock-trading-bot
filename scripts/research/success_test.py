# Author: Kai0421
"""Print the bot's pass/fail verdict (docs/25-success-test.md). Read-only.

uv run python scripts/research/success_test.py [--db data/bot.db] [--since 2026-10-13]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bot.success_test import describe, evaluate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/bot.db"))
    ap.add_argument("--spy", type=Path, default=Path("data/bars/SPY.csv"))
    ap.add_argument("--since", default="2026-10-13", help="first day of swing mode")
    a = ap.parse_args()
    print(describe(evaluate(a.db, a.spy, a.since)))


if __name__ == "__main__":
    main()
