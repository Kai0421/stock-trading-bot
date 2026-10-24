# Author: Kai0421
"""Small helpers so every Slack message formats lists and tables the same way (pure).

Lists are bullets, one item per line; tabular data is an aligned table in a code block (Slack
shows code blocks in a fixed-width font, so the columns line up). Wording only.
"""

from __future__ import annotations

from collections.abc import Sequence


def bullets(items: Sequence[str]) -> str:
    """One bullet per line. Blank items are dropped."""
    return "\n".join(f"• {item}" for item in items if item and item.strip())


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """An aligned table in a code block, or "" when there are no rows."""
    if not rows:
        return ""
    cells = [list(map(str, headers)), *[list(map(str, r)) for r in rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def fmt(row: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()

    lines = [fmt(cells[0]), "-" * min(sum(widths) + 2 * (len(widths) - 1), 78)]
    lines += [fmt(r) for r in cells[1:]]
    return "```\n" + "\n".join(lines) + "\n```"


def section(heading: str, body: str) -> str:
    """A bold heading above its body; nothing when the body is empty."""
    return f"*{heading}*\n{body}" if body.strip() else ""
