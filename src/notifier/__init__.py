# Author: Kai0421
"""The notifier: sends Slack messages for the trading bot and the Council of Ricks.

Imports nothing from ``bot``; the bot depends on it, never the other way round.
"""

from notifier.checks import lint
from notifier.config import CATEGORIES, NotifierConfig
from notifier.core import (
    Delivery,
    Dispatcher,
    Level,
    MacNotifier,
    NotifyError,
    SlackBotNotifier,
    SlackWebhookNotifier,
    build_dispatcher,
    slack_text,
)
from notifier.state import State

__all__ = [
    "CATEGORIES",
    "Delivery",
    "Dispatcher",
    "Level",
    "MacNotifier",
    "NotifierConfig",
    "NotifyError",
    "SlackBotNotifier",
    "SlackWebhookNotifier",
    "State",
    "build_dispatcher",
    "lint",
    "slack_text",
]
