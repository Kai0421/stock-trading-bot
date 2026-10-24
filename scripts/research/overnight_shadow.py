# Author: Kai0421
"""Overnight hold shadow log (research only, market data reads, NO orders).

Each trading day after the close it records what the overnight rules would buy at the
close, and once the next session has opened it records the sale at that open. Nothing is
sent to the broker. Two variants run side by side:
  - "universe top9": walk-forward monthly list from the bot's universe (the fair test)
  - "chips fixed": the hand-picked nine chip stocks

    uv run python scripts/research/overnight_shadow.py run      # safe to call repeatedly
    uv run python scripts/research/overnight_shadow.py report   # track record so far

Log: data/research/overnight_shadow.csv. Report: data/research/overnight_shadow.md.
launchd runs "run" every 30 minutes (launchd/com.stocktradingbot.overnight-shadow...).
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from bot.config import Settings, load_env_file
from bot.data import ET, load_daily_bars, make_data_client
from bot.overnight_hold import (
    CHIP_BASKET,
    SHADOW_COLUMNS,
    OvernightConfig,
    record_shadow,
    settle_shadow,
    shadow_summary,
)
from bot.universe import symbols as universe_symbols

LOG = Path("data/research/overnight_shadow.csv")
REPORT = Path("data/research/overnight_shadow.md")
HISTORY_DAYS = 420  # enough for the 252-night selection lookback
RECORD_AFTER = (16, 20)  # ET; the daily bar's close is final by then
SETTLE_AFTER = (9, 35)  # ET; the session's open is known
# Alpaca's free plan refuses SIP data from the latest 15 minutes, so bars stop 16 minutes
# ago. The open is settled on the first run after 09:46.
SIP_DELAY = timedelta(minutes=16)


def variants() -> dict[str, OvernightConfig]:
    pool = tuple(universe_symbols())
    return {
        "universe top9": OvernightConfig(symbols=pool, select_top=9),
        "chips fixed": OvernightConfig(symbols=CHIP_BASKET),
    }


def read_log() -> pd.DataFrame:
    if not LOG.exists():
        return pd.DataFrame(columns=list(SHADOW_COLUMNS))
    return pd.read_csv(LOG, parse_dates=["buy_date", "sell_date"])


def write_log(log: pd.DataFrame) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log.sort_values(["buy_date", "variant", "symbol"]).to_csv(LOG, index=False)


def _after(now: datetime, hm: tuple[int, int]) -> bool:
    return now.weekday() < 5 and (now.hour, now.minute) >= hm


def run(now: datetime) -> int:
    log = read_log()
    today = pd.Timestamp(now.date())
    buys = pd.to_datetime(log["buy_date"]) if len(log) else pd.Series(dtype="datetime64[ns]")
    open_rows = pd.isna(log["sell_open"]) if len(log) else pd.Series(dtype=bool)
    need_settle = bool((open_rows & (buys < today)).any()) and _after(now, SETTLE_AFTER)
    recorded = bool((buys == today).any())
    need_record = _after(now, RECORD_AFTER) and not recorded
    if not (need_settle or need_record):
        return 0
    settings = Settings.from_env({**load_env_file(Path(".env")), **os.environ})
    vs = variants()
    syms = sorted({s for cfg in vs.values() for s in cfg.symbols})
    res = load_daily_bars(
        make_data_client(settings),
        syms,
        (now - timedelta(days=HISTORY_DAYS)).date(),
        now - SIP_DELAY,
        cache=None,
    )
    bars = res.bars
    for name, cfg in vs.items():
        log = settle_shadow(log, bars, cfg) if not log.empty else log
        if need_record:
            log = record_shadow(log, bars, cfg, name, today)
    write_log(log)
    print(f"{now:%Y-%m-%d %H:%M %Z}: {len(log)} rows, {int(pd.isna(log['sell_open']).sum())} open")
    return 0


def report() -> int:
    log = read_log()
    if log.empty:
        print("no shadow trades yet")
        return 0
    cfg = OvernightConfig()
    table = shadow_summary(log, cfg).round(4)
    first = pd.to_datetime(log["buy_date"]).min().date()
    lines = [
        f"# Overnight hold shadow log (no orders), since {first}",
        "",
        "```",
        table.to_string(),
        "```",
        "",
        "Gate: 100+ settled trades with lower_95 >= 0.60 and avg_return > 0.",
        "",
        "## Last 10 nights",
        "",
        "```",
        log.sort_values("buy_date").tail(10).to_string(index=False),
        "```",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="overnight_shadow.py")
    ap.add_argument("command", choices=["run", "report"])
    args = ap.parse_args(argv)
    if args.command == "report":
        return report()
    return run(datetime.now(ET))


if __name__ == "__main__":
    sys.exit(main())
