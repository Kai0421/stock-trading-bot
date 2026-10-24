# Author: Kai0421
"""Tell the owner once when the bot or the council reaches 30 finished trades (pure helpers)."""

from __future__ import annotations

from collections.abc import Mapping

AT = 30
NAMES = {"bot": "The trading bot", "council": "The Council of Ricks"}


def due(counts: Mapping[str, int | None], told: Mapping[str, int], at: int = AT) -> list[str]:
    """Systems that reached ``at`` finished trades and have not been told about it yet."""
    return [
        name
        for name, n in sorted(counts.items())
        if n is not None and n >= at and told.get(name, 0) < at
    ]


def message(system: str, n: int, results: str, at: int = AT) -> tuple[str, str]:
    who = NAMES[system]
    title = f"{who} has reached {at} finished trades"
    text = "\n".join(
        [
            f"You asked to be told at {at} finished trades: {who} now has {n} finished.",
            results,
            "This is when charts start to be worth it (account value over time against SPY, "
            "and the council against the bot) and when a first fair comparison becomes possible. "
            "Ask and I will build them.",
            "No real order was placed; this is only a progress note.",
        ]
    )
    return title, text
