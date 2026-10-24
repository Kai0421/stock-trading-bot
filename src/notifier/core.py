# Author: Kai0421
"""Sending: Slack (bot token or webhook) with a macOS notification as the last resort.

This package is the notifier. It does not import anything from the trading bot, has its own
settings (``config``) and its own small state file (``state``), so it can be changed and deployed
on its own. ``Dispatcher.notify`` never raises: a failed message must not crash a trading job.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

from notifier.checks import lint
from notifier.config import CATEGORIES, NotifierConfig  # noqa: F401

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
TIMEOUT = 5
DEDUPE_WINDOW = timedelta(minutes=30)


class Level(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class NotifyError(RuntimeError):
    pass


class Notifier(Protocol):
    name: str

    def send(
        self,
        level: Level,
        title: str,
        text: str,
        files: Sequence[Path] = (),
        thread: str | None = None,
    ) -> str | None: ...


LEVEL_ICON = {Level.INFO: "", Level.WARN: ":warning: ", Level.CRITICAL: ":rotating_light: "}


def slack_text(level: Level, title: str, text: str) -> str:
    """The Slack message: an icon only for warnings, the title in bold, then the details.
    CRITICAL alerts also mention ``<!channel>`` so they ping."""
    prefix = "<!channel> " if level is Level.CRITICAL else ""
    return f"{prefix}{LEVEL_ICON[level]}*{title}*\n{text}".rstrip()


def _check(resp: Any, channel: str) -> None:
    if not 200 <= resp.status_code < 300:
        raise NotifyError(f"{channel} returned HTTP {resp.status_code}")


class SlackWebhookNotifier:
    """Incoming webhook: text only (webhooks cannot upload files or reply in threads)."""

    name = "slack-webhook"

    def __init__(self, url: str, post: Callable[..., Any] = requests.post) -> None:
        self._url = url
        self._post = post

    def send(self, level, title, text, files=(), thread=None):
        body = slack_text(level, title, text)
        _check(self._post(self._url, json={"text": body}, timeout=TIMEOUT), self.name)
        return None


class SlackBotNotifier:
    """Bot token (chat:write, files:write): threads and file uploads via files_upload_v2."""

    name = "slack-bot"

    def __init__(self, token: str, channel: str, client: Any = None) -> None:
        if client is None:
            import ssl

            import certifi
            from slack_sdk import WebClient

            # python.org Python on macOS has no system CA store; use certifi like requests does.
            ctx = ssl.create_default_context(cafile=certifi.where())
            client = WebClient(token=token, timeout=TIMEOUT, ssl=ctx)
        self._client = client
        self._channel = channel

    def send(self, level, title, text, files=(), thread=None):
        kwargs: dict[str, Any] = {"channel": self._channel, "text": slack_text(level, title, text)}
        if thread:
            kwargs["thread_ts"] = thread
        resp = self._client.chat_postMessage(**kwargs)
        if not resp.get("ok"):
            raise NotifyError(f"slack chat.postMessage failed: {resp.get('error')}")
        ts = resp.get("ts")
        if files:
            # files_upload_v2 needs the channel ID, which chat.postMessage returns.
            up = self._client.files_upload_v2(
                channel=resp.get("channel"),
                thread_ts=ts,
                file_uploads=[{"file": str(f), "title": Path(f).name} for f in files],
            )
            if not up.get("ok"):
                raise NotifyError(f"slack files_upload_v2 failed: {up.get('error')}")
        return ts


class MacNotifier:
    name = "macos"

    def __init__(self, run: Callable[..., Any] = subprocess.run) -> None:
        self._run = run

    def send(self, level, title, text, files=(), thread=None):
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{esc(text[:200])}" with title "{esc(title)}"'
        self._run(["osascript", "-e", script], check=True, timeout=TIMEOUT)
        return None


@dataclass(frozen=True)
class Delivery:
    ok: bool
    ref: str | None  # Slack message ts when available (for threads)


@dataclass
class Dispatcher:
    slack: list[Any]  # the trading bot's channel (the default)
    fallback: list[Any]
    dedupe: Callable[[str], bool] | None = None
    retries: int = 1
    routes: dict[str, list[Any]] = field(default_factory=dict)  # category -> notifiers
    check: bool = True  # read each message before sending (bot/notice_check.py)
    default_category: str = "bot"  # the category used when notify() is given none
    _log: logging.Logger = field(default=log, repr=False)

    def _try(self, n: Any, *args: Any, **kwargs: Any) -> tuple[bool, str | None]:
        for attempt in range(1 + self.retries):
            try:
                return True, n.send(*args, **kwargs)
            except Exception as exc:  # a failed alert must never crash a job
                self._log.warning("notifier %s failed (attempt %d): %s", n.name, attempt + 1, exc)
        return False, None

    def notify(
        self,
        level: Level,
        title: str,
        text: str = "",
        files: Sequence[Path] = (),
        thread: str | None = None,
        category: str | None = None,
    ) -> Delivery:
        """Send to Slack, falling back to macOS. Never raises. With no ``category`` the message
        goes to this dispatcher's own default channel (the one it was built for); naming a
        category sends it to that category's channel. An unknown category uses the default."""
        try:
            problems = lint(title, text) if self.check else []
            if problems and level is Level.INFO:
                # A report or plan that looks wrong is not sent; the owner is told instead.
                self._log.error("MESSAGE HELD BACK: %s: %s", title, problems)
                self._deliver(
                    Level.WARN,
                    "A message was held back",
                    f'The message "{title}" looked wrong ({"; ".join(problems)}), so it was '
                    "not sent. The bot's logs have the details.",
                    (),
                    None,
                    "general",
                )
                return Delivery(False, None)
            if problems:  # warnings and critical alerts are never held back
                text = (
                    f"{text}\n:warning: An automatic check found a problem with this message "
                    f"({'; '.join(problems)}). Treat it with care."
                )
            key = hashlib.sha256(f"{level}|{title}|{text}".encode()).hexdigest()
            if self.dedupe is not None and not self.dedupe(key):
                return Delivery(True, None)
            result = self._deliver(level, title, text, files, thread, category)
            if (
                not result.ok
                and (category or self.default_category) != "bot"
                and "bot" in self.routes
            ):
                self._deliver(
                    Level.WARN,
                    "A message could not be delivered",
                    f'"{title}" could not be sent to its own channel, and it was not posted '
                    "anywhere else. The bot's logs have the details.",
                    (),
                    None,
                    "bot",
                )
            return result
        except Exception as exc:
            self._log.error("dispatcher error: %s", exc)
            return Delivery(False, None)

    def _deliver(
        self,
        level: Level,
        title: str,
        text: str,
        files: Sequence[Path],
        thread: str | None,
        category: str | None,
    ) -> Delivery:
        try:
            args = (level, title, text)
            delivered, ref = False, None
            for n in (self.routes.get(category) if category else None) or self.slack:
                delivered, ref = self._try(n, *args, files=files, thread=thread)
                if delivered:
                    break
            if not delivered:
                for n in self.fallback:
                    if self._try(n, *args)[0]:
                        delivered = True
                        break
            if not delivered:
                self._log.error("ALERT NOT DELIVERED [%s] %s: %s", level, title, text)
            return Delivery(delivered, ref)
        except Exception as exc:
            self._log.error("dispatcher error: %s", exc)
            return Delivery(False, None)


def build_dispatcher(
    config: NotifierConfig,
    dedupe: Callable[[str], bool] | None = None,
    category: str = "bot",
    check: bool = True,
) -> Dispatcher:
    """A dispatcher whose default channel is ``category``'s; it can still send to the other
    categories' channels by name. An unset channel falls back to the trading bot's. The webhook
    is a backup for the trading bot's channel only (it always posts there)."""

    def notifiers(category_: str, channel: str) -> list[Any]:
        out: list[Any] = []
        if config.slack_bot_token:
            out.append(SlackBotNotifier(config.slack_bot_token, channel))
        # The webhook always posts to the trading bot's channel, so it is a backup there only.
        # For other categories it is used only when it is the sole way to reach Slack.
        if config.slack_webhook_url and (category_ == "bot" or not config.slack_bot_token):
            out.append(SlackWebhookNotifier(config.slack_webhook_url))
        return out

    # The macOS notification only exists on a Mac (not on a Linux server or GitHub runner).
    fallback = [MacNotifier()] if sys.platform == "darwin" else []
    routes = {c: notifiers(c, config.channel(c)) for c in CATEGORIES}
    return Dispatcher(
        slack=routes.get(category) or routes["bot"],
        fallback=fallback,
        dedupe=dedupe,
        routes=routes,
        check=check,
        default_category=category if category in routes else "bot",
    )
