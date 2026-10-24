# Author: Kai0421
"""Text checks for a message before it is sent (pure). Knows nothing about trading."""

from __future__ import annotations

import re

RAW_CODES = ("time_stop", "trend_break", "profit_target", "bounce_exit", "partial_profit")

_LINT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bNone\b"), "the word None appears"),
    (re.compile(r"\bnan\b", re.I), "a number shows as nan"),
    (re.compile(r"[{}]"), "unfilled braces appear"),
    (
        re.compile(r"\b(" + "|".join(RAW_CODES) + r")\b"),
        "an internal code is shown instead of words",
    ),
    (re.compile(r"Traceback \(most recent call last\)"), "a Python error trace appears"),
    (re.compile(r"\b(?:1[0-9]{2}|[2-9][0-9]{2}|[0-9]{4,})% win"), "a win chance is over 100%"),
    (re.compile(r"(?<![\w.])-\d+ shares?\b"), "a negative number of shares appears"),
)


def lint(title: str, text: str) -> list[str]:
    """Problems that make a message look wrong, found by reading it. Empty when it looks fine."""
    body = f"{title}\n{text}"
    return [why for pattern, why in _LINT if pattern.search(body)]
