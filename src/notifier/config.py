# Author: Kai0421
"""The notifier's own settings: read from the environment or a ``.env`` file, nothing else.

It knows nothing about trading. Channels per category, the Slack bot token and webhook, and
where its small state file lives.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES = ("bot", "council", "general")  # trading bot, Council of Ricks, health and reports


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``.env`` file (KEY=VALUE, optional ``export`` and quotes)."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.isidentifier():
            out[key] = value
    return out


@dataclass(frozen=True)
class NotifierConfig:
    slack_bot_token: str = field(default="", repr=False)
    slack_webhook_url: str = field(default="", repr=False)
    slack_channel: str = "#trading-bot"  # the trading bot's channel, and the fallback
    council_slack_channel: str = ""
    general_slack_channel: str = ""
    db_path: Path = Path("data/notifier.db")
    outbox_path: Path = Path("data/notifier_outbox.db")

    def channel(self, category: str) -> str:
        """The channel for a category; an unset one falls back to the trading bot's."""
        if category == "council":
            return self.council_slack_channel or self.slack_channel
        if category == "general":
            return self.general_slack_channel or self.slack_channel
        return self.slack_channel

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> NotifierConfig:
        return cls(
            slack_bot_token=env.get("SLACK_BOT_TOKEN", ""),
            slack_webhook_url=env.get("SLACK_WEBHOOK_URL", ""),
            slack_channel=env.get("SLACK_CHANNEL") or cls.slack_channel,
            council_slack_channel=env.get("COUNCIL_SLACK_CHANNEL", ""),
            general_slack_channel=env.get("GENERAL_SLACK_CHANNEL", ""),
            db_path=Path(env.get("NOTIFIER_DB") or cls.db_path),
            outbox_path=Path(env.get("NOTIFIER_OUTBOX") or cls.outbox_path),
        )

    @classmethod
    def load(cls, env_file: Path = Path(".env")) -> NotifierConfig:
        """The ``.env`` file in the current folder, overridden by real environment variables."""
        return cls.from_env({**load_env_file(env_file), **os.environ})
