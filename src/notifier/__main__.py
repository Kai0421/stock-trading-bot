# Author: Kai0421
"""Command line: ``python -m notifier --category general --level INFO --title T "text"``.

Uses only the notifier's own settings (.env in the current folder or real environment variables)
and its own state file. Exit code 0 when the message was delivered (or already sent recently).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from notifier.config import CATEGORIES, NotifierConfig
from notifier.core import DEDUPE_WINDOW, ET, Level, build_dispatcher
from notifier.outbox import Outbox, drain
from notifier.state import State


def run_drain(config: NotifierConfig, now: datetime) -> tuple[int, int]:
    """Send everything waiting in the queue; returns (sent, failed this round)."""
    state = State(config.db_path)
    outbox = Outbox(config.outbox_path)
    try:
        dispatcher = build_dispatcher(
            config, dedupe=lambda key: state.should_send(key, now, DEDUPE_WINDOW)
        )
        return drain(outbox, dispatcher, now)
    finally:
        state.close()


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["drain"]:  # python -m notifier drain
        sent, failed = run_drain(NotifierConfig.load(), datetime.now(ET))
        print(f"outbox: {sent} sent, {failed} could not be sent yet")
        return 0
    ap = argparse.ArgumentParser(prog="python -m notifier")
    ap.add_argument("--level", choices=[lv.value for lv in Level], default="INFO")
    ap.add_argument("--title", default="")
    ap.add_argument("--file", type=Path, help="post this file's contents")
    ap.add_argument("--thread", help="reply in a stored thread, e.g. 'weekly'")
    ap.add_argument("--category", choices=CATEGORIES, default="bot")
    ap.add_argument("message", nargs="*")
    args = ap.parse_args(argv)

    text = " ".join(args.message)
    if args.file:
        text = (text + "\n" + args.file.read_text()).strip()
    title = args.title or (args.file.stem if args.file else "Notification")
    config = NotifierConfig.load()
    state = State(config.db_path)
    try:
        dispatcher = build_dispatcher(
            config,
            dedupe=lambda key: state.should_send(key, datetime.now(ET), DEDUPE_WINDOW),
            category=args.category,
        )
        thread = state.get_kv(f"slack_thread:{args.thread}") if args.thread else None
        delivery = dispatcher.notify(Level(args.level), title, text, thread=thread)
    finally:
        state.close()
    if not delivery.ok:
        print("notification not delivered", file=sys.stderr)
    return 0 if delivery.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
