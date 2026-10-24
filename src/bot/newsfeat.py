# Author: Kai0421
"""News features for the council shadow log: count headlines and flag risk words per stock.

Pure functions, no network (``scripts/research/council_shadow.py`` fetches the Alpaca news).
Research only: these columns are logged beside the votes and change no vote and no order.
The window ends at the 15:50 ET overnight buy time, so later news cannot leak into a day.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime

RISK_WORDS = (
    r"downgrad\w*|cuts? (?:its )?(?:price )?(?:target|guidance|outlook|forecast)|"
    r"lowers? (?:its )?(?:price )?(?:target|guidance|outlook|forecast)|"
    r"guidance cut|profit warning|warns?|probe|investigat\w*|subpoena\w*|lawsuit|sues|sued|"
    r"recall\w*|plunge\w*|tumbl\w*|slump\w*|sinks?|halt\w*|bankrupt\w*|fraud|"
    r"short[- ]seller|antitrust|delist\w*|layoffs?|data breach|outage"
)
EARNINGS_WORDS = (
    r"earnings|quarterly results|reports? (?:q[1-4]|results)|\beps\b|to report|"
    r"revenue (?:beat|miss)|q[1-4] results"
)
RISK_RE = re.compile(rf"\b(?:{RISK_WORDS})", re.IGNORECASE)
EARNINGS_RE = re.compile(rf"\b(?:{EARNINGS_WORDS})", re.IGNORECASE)
MAX_TAGGED = 5  # a headline tagged with more stocks is a market roundup, not stock news
COLUMNS = ("news_n", "news_risk_n", "news_earn_n", "news_risk_heads")


def classify(headline: str) -> tuple[bool, bool]:
    """(has a risk word, mentions earnings)."""
    return bool(RISK_RE.search(headline)), bool(EARNINGS_RE.search(headline))


def features(
    items: Iterable[Mapping], symbols: Iterable[str], start: datetime, end: datetime
) -> dict[str, dict]:
    """Per symbol: headline count, risk-flagged count, earnings-flagged count, risk headlines.

    ``items`` are Alpaca news records ({"headline", "symbols", "created_at" (ISO, UTC)}).
    Only items created in ``[start, end)`` count. Every symbol in ``symbols`` gets a row, with
    zeros when nothing was published (no news is not missing data).
    """
    out = {
        s: {"news_n": 0, "news_risk_n": 0, "news_earn_n": 0, "news_risk_heads": []} for s in symbols
    }
    for it in items:
        tags = it.get("symbols") or []
        if len(tags) > MAX_TAGGED:
            continue
        created = datetime.fromisoformat(str(it["created_at"]).replace("Z", "+00:00"))
        if not (start <= created < end):
            continue
        risk, earn = classify(it.get("headline", ""))
        for sym in tags:
            row = out.get(sym)
            if row is None:
                continue
            row["news_n"] += 1
            row["news_risk_n"] += int(risk)
            row["news_earn_n"] += int(earn)
            if risk and len(row["news_risk_heads"]) < 2:
                row["news_risk_heads"].append(it["headline"][:90])
    for row in out.values():
        row["news_risk_heads"] = " | ".join(row["news_risk_heads"])
    return out
