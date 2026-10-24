# Author: Kai0421
"""Alerts: Slack (bot token or webhook), with a macOS notification as the last resort.

docs/06-alerts.md:
- Every level goes to Slack; CRITICAL alerts mention ``<!channel>``.
- Identical alerts within 30 minutes are de-duplicated.
- Each channel gets a 5 s timeout and one retry. If Slack fails, the alert falls back to
  a macOS notification and the log. ``Dispatcher.notify`` never raises: a failed alert
  must not crash a trading job.

CLI (used by the AI review prompts):
``python -m bot.notify --level INFO --file memory/journal/2026-10-05.md [--thread weekly]``
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.config import Settings, load_env_file
from bot.journal import Journal
from notifier import (
    CATEGORIES,
    Delivery,
    Dispatcher,
    Level,
    MacNotifier,
    NotifierConfig,
    NotifyError,
    SlackBotNotifier,
    SlackWebhookNotifier,
    slack_text,
)
from notifier import build_dispatcher as build_notifier
from notifier.core import DEDUPE_WINDOW, ET, LEVEL_ICON, TIMEOUT, Notifier

# The sending code now lives in the notifier package; these names stay importable from here so
# the rest of the bot (and its tests) keep working unchanged.
__all__ = [
    "CATEGORIES",
    "DEDUPE_WINDOW",
    "ET",
    "LEVEL_ICON",
    "TIMEOUT",
    "Delivery",
    "Dispatcher",
    "Level",
    "MacNotifier",
    "Notifier",
    "NotifierConfig",
    "NotifyError",
    "SlackBotNotifier",
    "SlackWebhookNotifier",
    "slack_text",
]

log = logging.getLogger(__name__)


def usd(amount: float) -> str:
    """Dollars for people: ``$1,234.50``, ``-$12.00``."""
    return f"-${-amount:,.2f}" if amount < 0 else f"${amount:,.2f}"


def shares(n: float) -> str:
    n = int(n) if float(n).is_integer() else n
    return f"{n} share" if n == 1 else f"{n} shares"


EXIT_REASONS = {
    "stop": "the stop-loss was hit",
    "trail": "the trailing stop was hit",
    "partial_profit": "taking some profit",
    "open": "overnight trade, sold just after the open",
    "late": "overnight trade, sold after the open",
    "flatten": "closing out before the end of the day",
    "manual": "sold outside the bot",
    "sentiment": "Stocktwits sentiment turned sharply negative",
}


def reason_text(code: str) -> str:
    """A strategy's exit code (``time_stop``) in words (``time stop``)."""
    return EXIT_REASONS.get(code, code.replace("_", " "))


JOB_NAMES = {
    "premarket": "morning check",
    "entries": "buying",
    "manage": "stop-loss and exit check",
    "eod": "end-of-day wrap-up",
    "usage_watch": "Claude usage check",
    "review": "AI review",
    "weekly_report": "weekly report",
    "weekly_review": "weekly AI review",
    "refresh_evidence": "backtest refresh",
    "scan": "intraday scan",
    "flatten": "end-of-day close-out",
    "open_sell": "sale just after the open",
    "open_check": "check after the open",
    "close_buy": "buy before the close",
}


def job_name(job: str) -> str:
    """A scheduler job name in words (``close_buy`` -> ``buy before the close``)."""
    return JOB_NAMES.get(job, job.replace("_", " "))


# (pattern, wording): internal reason texts from the risk and gate code, in plain English.
_PLAIN = (
    (
        r"max per sector reached for (.+)",
        lambda s: f"the bot already holds the most it is allowed in the {s} sector",
    ),
    (
        r"max open positions \((\d+)\) reached",
        lambda n: f"the bot already holds its maximum of {n} positions",
    ),
    (
        r"not enough tradable cash",
        lambda: (
            "there isn't enough spare cash after the safety reserve and the money "
            "set aside for open orders"
        ),
    ),
    (r"kill switch: (.+)", lambda why: f"a safety stop is on ({why}), so there are no new buys"),
    (
        r"negative cash \(margin used\): buys blocked, manual review",
        lambda: (
            "cash is negative, which would mean borrowing money, so buying is blocked "
            "until you check the account"
        ),
    ),
    (
        r"margin/shorting lock not verified: buys blocked",
        lambda: "the bot couldn't confirm the account is cash-only, so buying is blocked",
    ),
    (
        r"account fetch failed: no orders",
        lambda: "the bot couldn't read the account, so it placed no orders",
    ),
    (
        r"(\w+): duplicate in batch",
        lambda sym: f"{sym} was listed twice, so it was bought only once",
    ),
    (
        r"(\w+): unknown sector",
        lambda sym: f"the bot doesn't know which sector {sym} is in, so it skipped it to be safe",
    ),
    (
        r"(\w+): invalid stop ([\d.]+)",
        lambda sym, stop: (
            f"the stop-loss price for {sym} (${float(stop):,.2f}) wasn't valid, "
            "so the bot skipped it"
        ),
    ),
    (
        r"(\w+): asset class '(.+)' not allowed",
        lambda sym, kind: f"{sym} is a {kind}, and the bot only buys ordinary US stocks and ETFs",
    ),
    (
        r"(\w+): leveraged/inverse/volatility product",
        lambda sym: f"{sym} is a leveraged or inverse fund, which the bot never buys",
    ),
    (r"(\w+): not in the whitelist", lambda sym: f"{sym} isn't on the bot's approved list"),
    (
        r"win rate ([\d.]+)% below target (\d+)%",
        lambda got, need: f"the chance of winning ({got}%) is below the required {need}%",
    ),
    (
        r"win rate below target (\d+)%",
        lambda need: f"the chance of winning is below the required {need}%",
    ),
    (
        r"expectancy -?[\d.]+R is not positive",
        lambda: "on past results this kind of setup loses a little money per trade on average",
    ),
    (
        r"95% lower bound of mean R .* is not positive",
        lambda: "past results aren't strong enough to trust",
    ),
    (
        r"95% lower bound ([\d.]+)% below floor (\d+)%",
        lambda got, need: (
            f"past results aren't reliable enough (the worst case win rate is "
            f"{got}%, {need}% is required)"
        ),
    ),
    (
        r"95% lower bound below floor (\d+)%",
        lambda need: (
            "past results aren't reliable enough "
            f"(the worst case win rate must be at least {need}%)"
        ),
    ),
    (
        r"only (\d+) matching trades \(need (\d+)\)",
        lambda n, need: (
            f"there are only {n} similar past trades, and {need} are needed to trust the numbers"
        ),
    ),
    (
        r"no R spread in the evidence; re-run backtest",
        lambda: "the backtest results are incomplete",
    ),
    (
        r"7-day usage (\d+)% >= (\d+)%",
        lambda used, cap: f"Claude's weekly usage ({used}%) is above the {cap}% limit",
    ),
    (
        r"5-hour usage (\d+)% >= (\d+)%",
        lambda used, cap: f"Claude's 5-hour usage ({used}%) is above the {cap}% limit",
    ),
)


def sentence(text: str) -> str:
    """Capital letter first, full stop last."""
    t = text.strip()
    if not t:
        return t
    t = t[0].upper() + t[1:]
    return t if t.endswith((".", "!", "?")) else t + "."


def plain_reason(reason: str) -> str:
    """A rejection or gate reason in plain English; unknown reasons pass through."""
    import re

    text = reason.strip()
    for pattern, wording in _PLAIN:
        m = re.fullmatch(pattern, text)
        if m:
            return wording(*m.groups())
    return reason


def plan_table(signals: list[dict[str, Any]], plans: list[Any], limit: int = 10) -> str:
    """The day's possible trades as an aligned table (for a Slack code block).

    Planned buys first in plan order, then skipped setups by win chance. Wording only.
    """
    rows: dict[str, tuple[int, float, str, str]] = {}
    win: dict[str, float] = {}
    by_symbol = {sig["symbol"]: sig for sig in signals}
    for i, p in enumerate(plans, 1):
        rows[p.symbol] = (
            0,
            -p.win_prob,
            f"Buy (plan #{i})",
            buy_reason(by_symbol.get(p.symbol, {})),
        )
        win[p.symbol] = p.win_prob
    for sig in signals:
        sym = sig["symbol"]
        if sym not in rows:
            prob = sig.get("win_prob") or 0.0
            raw = str(sig.get("reason") or "")
            full = raw.startswith("max per sector")
            status = "Skipped (sector full)" if full else "Skipped"
            rows[sym] = (1, -prob, status, skip_reason(raw))
            win[sym] = prob
    ordered = sorted(rows.items(), key=lambda kv: kv[1][:2])
    lines = [f"{'Stock':<7}{'Win chance':<12}{'Decision':<23}Reason", "-" * 78]
    for sym, (_, _, status, why) in ordered[:limit]:
        chance = f"{win[sym]:.0%}" if win[sym] > 0 else "n/a"
        lines.append(f"{sym:<7}{chance:<12}{status:<23}{why}")
    if len(ordered) > limit:
        lines.append(f"... and {len(ordered) - limit} more skipped")
    return "```\n" + "\n".join(lines) + "\n```"


REASON_WIDTH = 36


def _clip(text: str, width: int = REASON_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


def buy_reason(sig: dict[str, Any]) -> str:
    """Why a setup is a buy, in a few plain words, from the numbers the journal kept for it.
    Wording only; it never decides anything."""
    import json

    try:
        f = json.loads(sig.get("features") or "{}")
    except (TypeError, ValueError):
        return "Met the buy rules"
    rsi, mom = f.get("rsi_min"), f.get("momentum")
    if not isinstance(rsi, int | float) and not isinstance(mom, int | float):
        return "Met the buy rules"
    parts = ["Uptrend"]
    if isinstance(rsi, int | float):
        parts.append("sharp dip" if rsi < 30 else "dip" if rsi < 50 else "pullback to its average")
    if isinstance(mom, int | float):
        parts.append(f"up {mom:.0%} in 6 mo")
    return _clip(", ".join(parts))


def skip_reason(raw: str) -> str:
    """Why a setup was not bought, short and plain."""
    if raw.startswith("max per sector"):
        return "sector already holds the limit"
    if not raw or raw == "planned":
        return "no room in today's plan"
    return _clip(sentence(plain_reason(raw)).rstrip("."))


SALE_REASONS = {
    "trend_break": "the uptrend broke",
    "time_stop": "held the maximum number of days",
    "profit_target": "reached its profit target",
    "bounce_exit": "bounced back to its average",
    "partial_profit": "taking some profit",
}


def sale_table(positions: Sequence[Any], limit: int = 10) -> str:
    """Today's planned sales (decided at yesterday's close, sold at the open) as an aligned
    table, with the reason in plain English. Empty when nothing is planned."""
    rows = [p for p in positions if p.pending_exit is not None]
    if not rows:
        return ""
    lines = [f"{'Stock':<7}{'Shares':<8}Reason", "-" * 60]
    for p in sorted(rows, key=lambda p: p.symbol)[:limit]:
        kind, qty = p.pending_exit
        why = SALE_REASONS.get(kind) or EXIT_REASONS.get(kind) or kind.replace("_", " ")
        lines.append(f"{p.symbol:<7}{qty:<8}{why} (day {p.days_held})")
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more")
    return "```\n" + "\n".join(lines) + "\n```"


def build_dispatcher(
    settings: Settings,
    journal: Journal | None,
    now: Callable[[], datetime] | None = None,
    council: bool = False,
    category: str | None = None,
) -> Dispatcher:
    """The notifier's dispatcher, configured from the bot's settings. De-duplication uses the
    trading journal here; the standalone notifier (``python -m notifier``) uses its own file."""
    clock = now or (lambda: datetime.now(ET))
    config = NotifierConfig(
        slack_bot_token=settings.slack_bot_token,
        slack_webhook_url=settings.slack_webhook_url,
        slack_channel=settings.slack_channel,
        council_slack_channel=settings.council_slack_channel,
        general_slack_channel=settings.general_slack_channel,
    )
    dedupe = None
    if journal is not None:
        dedupe = lambda key: journal.should_send(key, clock(), DEDUPE_WINDOW)  # noqa: E731
    return build_notifier(
        config, dedupe=dedupe, category=category or ("council" if council else "bot")
    )


def make_notifier(settings: Settings, journal: Journal | None) -> Any:
    """What the engine sends through: the direct dispatcher, or (NOTIFY_VIA_OUTBOX) a queue that
    a separate task drains, with the direct dispatcher as the rescue path."""
    direct = build_dispatcher(settings, journal)
    if not settings.notify_via_outbox:
        return direct
    from notifier.outbox import Outbox, OutboxDispatcher

    return OutboxDispatcher(
        Outbox(settings.notifier_outbox), direct, clock=lambda: datetime.now(ET)
    )


def _dispatcher_from_env(category: str = "bot") -> tuple[Dispatcher, Journal]:
    import os

    settings = Settings.from_env({**load_env_file(Path(".env")), **os.environ})
    journal = Journal(settings.db_path)
    return build_dispatcher(settings, journal, category=category), journal


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.notify")
    ap.add_argument("--level", choices=[lv.value for lv in Level], default="INFO")
    ap.add_argument("--title", default="")
    ap.add_argument("--file", type=Path, help="post this file's contents")
    ap.add_argument("--thread", help="reply in a stored thread, e.g. 'weekly'")
    ap.add_argument(
        "--category",
        choices=CATEGORIES,
        default="bot",
        help="which channel: the trading bot's, the council's, or general (health and reports)",
    )
    ap.add_argument("--council", action="store_true", help="same as --category council")
    ap.add_argument("message", nargs="*")
    args = ap.parse_args(argv)
    category = "council" if args.council else args.category

    text = " ".join(args.message)
    if args.file:
        text = (text + "\n" + args.file.read_text()).strip()
    title = args.title or (args.file.stem if args.file else "StockTradingBot")
    dispatcher, journal = _dispatcher_from_env(category)
    thread = journal.get_kv(f"slack_thread:{args.thread}") if (args.thread and journal) else None
    delivery = dispatcher.notify(Level(args.level), title, text, thread=thread)
    if not delivery.ok:
        print("notification not delivered", file=sys.stderr)
    return 0 if delivery.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
