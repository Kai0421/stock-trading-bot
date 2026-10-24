# Author: Kai0421
"""Alpaca News API (Benzinga headlines): major-news shock detection for the entry veto."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

MAJOR_NEWS = re.compile(
    r"\b(merger|acquir\w*|acquisition|buyout|takeover|tender offer|fda|clinical hold|"
    r"(cuts?|lower(s|ed)?|withdraw(s|n)?|slash(es)?) (its |full[-]year )?guidance|"
    r"guidance cut|lawsuit|sued|subpoena|sec (probe|investigation)|investigation|"
    r"bankruptcy|chapter 11|recall|delist\w*|trading halt|halted|fraud|restatement|"
    r"going concern)\b",
    re.IGNORECASE,
)


def is_major(headline: str) -> bool:
    return bool(MAJOR_NEWS.search(headline or ""))


def fetch_news(
    client: Any, symbol: str, start: datetime, end: datetime
) -> list[tuple[int, datetime, str]]:
    """All headlines for ``symbol`` in [start, end), following pagination."""
    from alpaca.data.requests import NewsRequest

    out: list[tuple[int, datetime, str]] = []
    token = None
    while True:
        req = NewsRequest(symbols=symbol, start=start, end=end, limit=50, page_token=token)
        resp = client.get_news(req)
        for item in resp.data.get("news", []):
            out.append((int(item.id), item.created_at, item.headline))
        token = getattr(resp, "next_page_token", None)
        if not token:
            return out
