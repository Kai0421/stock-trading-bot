# Author: Kai0421
"""Council of Ricks paper ledger runner (research only: data reads and local files, NO orders).

Replays the council's shadow-log buy verdicts as pretend trades in a virtual $100,000 account
(rules in ``bot.ricks_paper``), writes the ledger, and tells Slack about every pretend buy and
sell plus a daily summary, all in plain English and labelled practice only.

    uv run python scripts/research/council_paper.py run            # update ledger + Slack
    uv run python scripts/research/council_paper.py run --print    # update ledger, no Slack
    uv run python scripts/research/council_paper.py run --mark-sent  # mark all past events as
                                                                     # already told (no Slack)

Safe to repeat: the ledger is recomputed from the log and bars; an event is told once (sent flag
in the ledger) and the daily summary once per session day. Never calls a broker.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from bot import notify
from bot import ricks_paper as rp
from bot.config import Settings, load_env_file
from bot.data import ET, load_daily_bars, make_data_client
from bot.universe import universe_for

SIP_DELAY = timedelta(minutes=16)  # the free plan refuses the latest 15 minutes of SIP data
FINAL_AFTER = (16, 20)  # ET; today's daily bar is final by then
SEND_FROM = (8, 30)  # ET; Slack events go out from here, before the open (DST-proof: see below)
WARMUP_DAYS = 70  # calendar days of bars before the first logged day (for ATR)


def read_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=rp.COLUMNS)
    return pd.read_csv(path, parse_dates=["signal_day", "entry_date", "exit_date"])


def read_raises(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=rp.RAISE_COLUMNS)
    return pd.read_csv(path, parse_dates=["day"])


def last_complete_session(now: datetime) -> pd.Timestamp:
    today = pd.Timestamp(now.date())
    if now.weekday() < 5 and (now.hour, now.minute) >= FINAL_AFTER:
        return today
    return today - pd.Timedelta(days=1)  # bars after this date may still be half a day


def slack(title: str, text: str) -> None:
    rc = notify.main(["--council", "--level", "INFO", "--title", title, text])
    if rc:
        raise RuntimeError(f"slack returned {rc}")


def send_due(now: datetime) -> bool:
    """Slack events are told from 08:30 ET. The task fires at 12:30 and 13:30 UK because UK and
    US clocks change on different dates; whichever run is first at or after 08:30 ET sends,
    and the 'told once' flags make the other one a no-op."""
    return (now.hour, now.minute) >= SEND_FROM


def tell_upgrade(state: dict, send=slack) -> None:
    """Post 'Council of Ricks upgraded' once when the rules version changes (not on first run)."""
    message = rp.upgrade_message(state.get("rules_version"))
    if message is None:
        state["rules_version"] = rp.RULES_VERSION
        return
    try:
        send(*message)
        state["rules_version"] = rp.RULES_VERSION
    except Exception as exc:
        print(f"upgrade message not sent ({type(exc).__name__}); will retry")


def run(args: argparse.Namespace, now: datetime) -> int:
    log = pd.read_csv(args.log, parse_dates=["day"]) if args.log.exists() else pd.DataFrame()
    if log.empty:
        print("no council shadow log yet; nothing to replay")
        return 0
    settings = Settings.from_env({**load_env_file(args.env), **os.environ})
    sectors = universe_for("sp100")
    wanted = sorted({*log.loc[log["buy"].astype(bool), "symbol"], "SPY"})
    bars = load_daily_bars(
        make_data_client(settings),
        wanted,
        (log["day"].min() - pd.Timedelta(days=WARMUP_DAYS)).date(),
        now - SIP_DELAY,
        cache=None,
    ).bars
    through = last_complete_session(now)
    res = rp.replay(log, bars, sectors, through=through)

    ledger_path = args.out / "council_paper.csv"
    equity_path = args.out / "council_paper_equity.csv"
    stops_path = args.out / "council_paper_stops.csv"
    state_path = args.out / "council_paper_state.json"
    trades = rp.merge_sent(res.trades, read_ledger(ledger_path))
    raises = rp.merge_sent_raises(res.raises, read_raises(stops_path))
    stats = rp.summary(trades, res.equity, res.marks)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    last_day = res.equity["day"].iloc[-1] if len(res.equity) else through

    if args.mark_sent:
        trades["buy_sent"] = 1
        trades.loc[trades["status"] == "closed", "sell_sent"] = 1
        raises["sent"] = 1
        state["summary_day"] = str(last_day.date())
        state["rules_version"] = rp.RULES_VERSION
    elif not args.print and not send_due(now):
        print("before 08:30 ET: ledger updated, Slack events wait for the next run")
    elif not args.print:
        tell_upgrade(state)
        trades, raises, n = rp.send_all(trades, raises, stats, slack)
        print(f"told Slack about {n} new pretend trade event(s)")
        if state.get("summary_day") != str(last_day.date()):
            try:
                slack(*rp.daily_message(last_day, trades, stats))
                state["summary_day"] = str(last_day.date())
            except Exception as exc:
                print(f"daily summary not sent ({type(exc).__name__}); will retry")

    args.out.mkdir(parents=True, exist_ok=True)
    trades.to_csv(ledger_path, index=False)
    raises.to_csv(stops_path, index=False)
    res.equity.to_csv(equity_path, index=False)
    state_path.write_text(json.dumps(state))
    show(trades, stats, last_day)
    return 0


def show(trades: pd.DataFrame, stats: dict, last_day: pd.Timestamp) -> None:
    spy = "n/a" if stats["spy_pct"] is None else f"{stats['spy_pct']:+.2%}"
    print(f"Council paper ledger through {last_day.date()} (practice only, no orders)")
    if len(trades):
        cols = ["symbol", "entry_date", "entry_price", "shares", "exit_date", "exit_price"]
        print(trades[[*cols, "exit_reason", "pnl"]].to_string(index=False))
    print(
        f"equity ${stats['equity']:,.0f} ({stats['return_pct']:+.2%}), "
        f"finished {stats['closed']}, open {stats['open']}, stops raised {stats['stops_raised']}, "
        f"SPY same period {spy}"
    )
    print(rp.verdict_line(stats))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="council_paper.py")
    ap.add_argument("command", choices=["run"])
    ap.add_argument("--print", action="store_true", help="no Slack; just update the ledger")
    ap.add_argument("--mark-sent", action="store_true", help="mark past events as already told")
    ap.add_argument("--log", type=Path, default=Path("data/research/council_shadow.csv"))
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--out", type=Path, default=Path("data/research"))
    args = ap.parse_args(argv)
    return run(args, datetime.now(ET))


if __name__ == "__main__":
    sys.exit(main())
