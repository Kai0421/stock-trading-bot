# Author: Kai0421
"""Council of Ricks shadow log (research only: market data and local files, NO orders).

Each trading day after the close it convenes the three Ricks on every universe stock, using the
newest fresh Stocktwits snapshots the collector wrote, and appends one row per stock. On every
run it also fills in the realised 1- and 5-day returns for older rows (entry at the next open).

    uv run python scripts/research/council_shadow.py run      # safe to call repeatedly
    uv run python scripts/research/council_shadow.py report   # what the Ricks are worth so far

Log: data/research/council_shadow.csv. Report: data/research/council_shadow.md.
Without a fresh snapshot for any stock the day is not recorded (a day of abstentions would
only dilute the log), and the next run tries again. Held positions are not modelled, so the
sector-full check never fires here.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from bot import newsfeat, notify
from bot import ricks_shadow as rs
from bot.config import Settings, load_env_file
from bot.data import ET, load_daily_bars, make_data_client
from bot.sentiment import latest_fresh, read_snapshots
from bot.universe import universe_for

LOG = Path("data/research/council_shadow.csv")
REPORT = Path("data/research/council_shadow.md")
SNAPSHOTS = Path("data/sentiment/snapshots.jsonl")
DIGEST_STATE = Path("data/research/council_digest_state.json")
DIGEST_AT = (8, 30)  # ET; the Slack digest goes out with the bot's morning plan, before the open
HISTORY_DAYS = 420
RECORD_AFTER = (16, 20)  # ET; the daily bar is final by then
SNAPSHOT_MAX_AGE_MIN = 300  # the collector's full pass starts at 15:30 ET
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
NEWS_HOURS = 24  # headlines in the 24 h before the 15:50 ET overnight buy
NEWS_PAGES = 30
SIP_DELAY = timedelta(minutes=16)  # the free plan refuses the latest 15 minutes of SIP data


def read_log() -> pd.DataFrame:
    if not LOG.exists():
        return pd.DataFrame()
    return pd.read_csv(LOG, parse_dates=["day"])


def write_log(log: pd.DataFrame) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log.sort_values(["day", "symbol"]).to_csv(LOG, index=False)


def fetch_news(settings: Settings, symbols: list[str], day: pd.Timestamp) -> dict | None:
    """Alpaca headlines for the 24 h before 15:50 ET on ``day`` -> newsfeat features.

    Read-only. Any failure returns None: the day is still logged, just without news columns.
    """
    end = datetime(day.year, day.month, day.day, 15, 50, tzinfo=ET).astimezone(UTC)
    start = end - timedelta(hours=NEWS_HOURS)
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
    }
    items: list[dict] = []
    token = None
    try:
        for _ in range(NEWS_PAGES):
            q = {
                "symbols": ",".join(symbols),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 50,
                "sort": "desc",
                "include_content": "false",
            }
            if token:
                q["page_token"] = token
            req = urllib.request.Request(f"{NEWS_URL}?{urllib.parse.urlencode(q)}", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                page = json.load(r)
            items += page.get("news", [])
            token = page.get("next_page_token")
            if not token:
                break
    except Exception as exc:  # news is optional; never lose the day's votes over it
        print(f"news fetch failed ({type(exc).__name__}); logging without news columns")
        return None
    print(f"news: {len(items)} headlines in the window")
    return newsfeat.features(items, symbols, start, end)


def digest_due(now: datetime, pending_day: str | None) -> bool:
    """True when a recorded day's digest is waiting and it is a later weekday, at or after 08:30 ET.

    The votes are logged whenever the data is ready (sometimes in the small hours); only the
    Slack message waits, so it lands just before the open when the buys would happen."""
    return (
        bool(pending_day)
        and now.date().isoformat() > str(pending_day)  # a later day than the one recorded
        and now.weekday() < 5
        and (now.hour, now.minute) >= DIGEST_AT
    )


def read_pending() -> str | None:
    try:
        return json.loads(DIGEST_STATE.read_text()).get("pending_day")
    except (OSError, ValueError):
        return None


def write_pending(day: str | None) -> None:
    DIGEST_STATE.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_STATE.write_text(json.dumps({"pending_day": day}))


def post_pending_digest(now: datetime, log: pd.DataFrame) -> None:
    day = read_pending()
    if not digest_due(now, day) or log.empty:
        return
    rows = log[log["day"] == pd.Timestamp(day)].to_dict("records")
    if rows:
        post_digest(rows)
    write_pending(None)


def post_digest(rows: list[dict]) -> None:
    """Slack post of the council's mock decisions. Information only; a failure is ignored."""
    title, text = rs.digest(rows)
    try:
        notify.main(["--council", "--level", "INFO", "--title", title, text])
    except Exception as exc:
        print(f"slack post failed ({type(exc).__name__})")


def run(now: datetime) -> int:
    log = read_log()
    today = pd.Timestamp(now.date())
    recorded = (not log.empty) and bool((log["day"] == today).any())
    after = now.weekday() < 5 and (now.hour, now.minute) >= RECORD_AFTER
    need_record = after and not recorded
    post_pending_digest(now, log)
    need_label = (not log.empty) and (
        "ret5" not in log or bool(log[["ret1", "ret5"]].isna().any().any())
    )
    if not (need_record or need_label):
        return 0
    settings = Settings.from_env({**load_env_file(Path(".env")), **os.environ})
    sectors = universe_for("sp100")
    res = load_daily_bars(
        make_data_client(settings),
        sorted({*sectors, "SPY"}),
        (now - timedelta(days=HISTORY_DAYS)).date(),
        now - SIP_DELAY,
        cache=None,
    )
    bars = res.bars
    if need_record and today in bars.get("SPY", pd.DataFrame()).index:
        snaps = latest_fresh(read_snapshots(SNAPSHOTS), sectors, now, SNAPSHOT_MAX_AGE_MIN)
        if snaps:
            news = fetch_news(settings, sorted(sectors), today)
            rows = rs.convene_day(today, snaps, bars, bars.get("SPY"), sectors, news=news)
            log = pd.concat([log, pd.DataFrame(rows)], ignore_index=True)
            print(f"recorded {len(rows)} stocks, {len(snaps)} with fresh sentiment")
            write_pending(str(today.date()))  # Slack digest waits for 08:30 ET
        else:
            print("no fresh Stocktwits snapshots yet; not recording today")
    if not log.empty:
        log = rs.label(log, bars)
        write_log(log)
    return 0


def report() -> int:
    log = read_log()
    if log.empty:
        print("no council shadow rows yet")
        return 0
    s = rs.summary(rs.add_excess(log))
    lines = [
        f"# Council of Ricks shadow log (no orders), since {log['day'].min().date()}",
        "",
        f"{log['day'].nunique()} days, {len(log)} stock-days, "
        f"{int(log['buy'].astype(bool).sum())} buy verdicts.",
        "",
        "Excess = return minus that day's average over the universe; entry at the next open.",
        "",
        "```",
        json.dumps(s, indent=2),
        "```",
        "",
        "News (Alpaca headlines in the 24 h before 15:50 ET; logged only, no vote uses it):",
        "```",
        json.dumps(rs.news_summary(rs.add_excess(log)) or "no labelled news rows yet", indent=2),
        "```",
        "",
        "Gate before any trading: 100+ settled buys with the win-rate lower bound >= 0.60",
        "and a positive edge over the average stock.",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="council_shadow.py")
    ap.add_argument("command", choices=["run", "report"])
    args = ap.parse_args(argv)
    if args.command == "report":
        return report()
    return run(datetime.now(ET))


if __name__ == "__main__":
    sys.exit(main())
