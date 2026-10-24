# Author: Kai0421
"""Checks a notification before it is sent (pure; wording and consistency only).

Two layers: ``lint`` looks for text that is plainly broken (placeholders, raw internal codes,
impossible numbers), and the ``check_*`` functions compare a plan with the rules it must obey.
Nothing here decides or places a trade. A check can hold back or flag a message, never a safety
alert: the dispatcher always sends WARN and CRITICAL messages, with the problem written on them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from notifier.checks import RAW_CODES, lint  # noqa: F401  (the text checks live in the notifier)


def check_plan(
    plans: Sequence[Any], positions: Sequence[Any], max_positions: int, max_per_sector: int
) -> list[str]:
    """Is the buy plan consistent with the position and sector limits and what is held?"""
    problems: list[str] = []
    slots = max(max_positions - len(positions), 0)
    if len(plans) > slots:
        problems.append(
            f"{len(plans)} buys planned but only {slots} slot{'s' if slots != 1 else ''} free"
        )
    held = {p.symbol for p in positions}
    problems += [f"the bot already holds {p.symbol}" for p in plans if p.symbol in held]
    sectors = Counter(p.sector for p in positions if getattr(p, "sector", ""))
    sectors.update(p.sector for p in plans if getattr(p, "sector", ""))
    problems += [
        f"{sector} would have {n} positions, over the limit of {max_per_sector}"
        for sector, n in sorted(sectors.items())
        if n > max_per_sector
    ]
    return problems


def check_sales(positions: Sequence[Any]) -> list[str]:
    """Is each planned sale for shares the bot actually holds?"""
    return [
        f"{p.symbol}: selling {p.pending_exit[1]} shares but only {p.shares} are held"
        for p in positions
        if p.pending_exit is not None and p.pending_exit[1] > p.shares
    ]
