# Author: Kai0421
"""Annual company fundamentals from SEC EDGAR XBRL ``companyfacts``, point in time.

Only 10-K full-year values (a ~1-year period, ``fp == "FY"``) filed before the as-of day
are used. When a later filing restates a year, the value first filed is kept: that is what
the market knew at the time, so a backtest sees the same numbers as a live run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
NET_INCOME_TAGS = ("NetIncomeLoss",)
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
YEAR_DAYS = (350, 380)

Series = tuple[tuple[date, float], ...]  # (fiscal year end, value), newest first


@dataclass(frozen=True)
class AnnualFacts:
    revenue: Series
    net_income: Series
    eps: Series


def _annual(facts: Mapping[str, Any], tags: Sequence[str], unit: str, as_of: date) -> Series:
    """Per fiscal year end, the first-filed value of the first tag that reports it."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    out: dict[date, float] = {}
    for tag in tags:
        first: dict[date, tuple[date, float]] = {}
        for row in gaap.get(tag, {}).get("units", {}).get(unit, []):
            try:
                start = date.fromisoformat(row["start"])
                end = date.fromisoformat(row["end"])
                filed = date.fromisoformat(row["filed"])
                value = float(row["val"])
            except (KeyError, TypeError, ValueError):
                continue
            if not str(row.get("form", "")).startswith("10-K") or row.get("fp") != "FY":
                continue
            if not YEAR_DAYS[0] <= (end - start).days <= YEAR_DAYS[1] or filed >= as_of:
                continue
            if end not in first or filed < first[end][0]:
                first[end] = (filed, value)
        for end, (_, value) in first.items():
            out.setdefault(end, value)
    return tuple(sorted(out.items(), reverse=True))


def annual_facts(facts: Mapping[str, Any], as_of: date) -> AnnualFacts:
    return AnnualFacts(
        revenue=_annual(facts, REVENUE_TAGS, "USD", as_of),
        net_income=_annual(facts, NET_INCOME_TAGS, "USD", as_of),
        eps=_annual(facts, EPS_TAGS, "USD/shares", as_of),
    )
