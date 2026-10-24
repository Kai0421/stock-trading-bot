# Author: Kai0421
"""Today's plan in one message: what to buy and what to sell (pure; wording only).

Built from the journal's plans, signals and positions. It decides nothing and places nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from bot.notify import plan_table, sale_table


def summary(
    day_label: str,
    premarket_ran: bool,
    plans: Sequence[Any],
    signals: list[dict[str, Any]],
    positions: Sequence[Any],
) -> tuple[str, str]:
    lines = ["Paper trading; no real money."]
    if not premarket_ran:
        lines.append(
            "Buy: today's buy plan is not made yet. The morning check runs at 08:30 ET "
            "(13:30 UK); buying happens between 09:45 and 11:00 ET."
        )
    elif plans:
        lines.append("Buy:\n" + plan_table(signals, list(plans)))
    else:
        lines.append("Buy: nothing planned today.")
    sales = sale_table(list(positions))
    if sales:
        lines.append("Sell (at today's open):\n" + sales)
    else:
        lines.append(
            "Sell: nothing planned today. The end-of-day check found no exit signal, so "
            "positions are only sold if their stop-loss triggers."
        )
    if positions:
        held = ", ".join(f"{p.symbol} {p.shares} shares (stop ${p.stop:,.2f})" for p in positions)
        lines.append(f"Holding {len(positions)}: {held}.")
    else:
        lines.append("No open positions.")
    return f"Today's plan, {day_label}: what to buy and sell", "\n".join(lines)
