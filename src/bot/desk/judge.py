# Author: Kai0421
"""The judge: combines analyst reports into one 5-tier rating, with risk caps.

Mirrors TradingAgents' roles without an LLM: the bull and bear cases are the positive and
negative points of every analyst, the risk manager applies caps, and the portfolio manager
turns a weighted score into Buy / Overweight / Hold / Underweight / Sell.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")  # most bullish first
# Lower bound of the weighted score for each rating above Sell.
THRESHOLDS = ((0.5, "Buy"), (0.2, "Overweight"), (-0.2, "Hold"), (-0.5, "Underweight"))
WEIGHTS = {"technical": 0.30, "fundamentals": 0.25, "market": 0.15, "insiders": 0.15, "news": 0.15}
MIN_ANALYSTS = 3
# Risk flag -> the most bullish rating allowed while it is raised.
CAPS = {
    "news_shock": ("Underweight", "Major news in the last 2 days"),
    "market_downtrend": ("Hold", "SPY is below its 200-day SMA"),
    "high_volatility": ("Hold", "Daily range (ATR) is above 4% of price"),
}


@dataclass(frozen=True)
class Report:
    analyst: str
    score: float  # -1 (bearish) .. +1 (bullish)
    points: tuple[tuple[float, str], ...] = ()  # (signed contribution, reason)
    available: bool = True
    note: str = ""  # why the analyst is unavailable
    flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    rating: str
    score: float
    confidence: float  # share of analyst weight that agrees with the final direction
    bull: tuple[str, ...]
    bear: tuple[str, ...]
    caps: tuple[str, ...]
    reports: tuple[Report, ...] = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rating": self.rating,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "bull": list(self.bull),
            "bear": list(self.bear),
            "caps": list(self.caps),
            "analysts": {
                r.analyst: {
                    "available": r.available,
                    "score": round(r.score, 3),
                    "note": r.note,
                    "flags": sorted(r.flags),
                }
                for r in self.reports
            },
        }

    def to_markdown(self, symbol: str, as_of: date) -> str:
        lines = [
            f"# {symbol}: {self.rating} (premarket {as_of.isoformat()})",
            "",
            f"Score {self.score:+.2f} on a -1 to +1 scale; confidence {self.confidence:.0%}.",
            "Research only: this rating never places or changes an order.",
            "",
            "| Analyst | Score | Notes |",
            "| --- | --- | --- |",
        ]
        for r in self.reports:
            score = f"{r.score:+.2f}" if r.available else "n/a"
            lines.append(f"| {r.analyst} | {score} | {r.note or ', '.join(sorted(r.flags))} |")
        lines += ["", "## Bull case", ""]
        lines += [f"- {p}" for p in self.bull] or ["- none"]
        lines += ["", "## Bear case", ""]
        lines += [f"- {p}" for p in self.bear] or ["- none"]
        if self.caps:
            lines += ["", "## Risk caps", ""]
            lines += [f"- {c}" for c in self.caps]
        return "\n".join(lines) + "\n"


def rating_for(score: float) -> str:
    for low, rating in THRESHOLDS:
        if score >= low:
            return rating
    return "Sell"


def _cap(rating: str, ceiling: str) -> str:
    """The more bearish of the two: a cap never makes a rating more bullish."""
    return RATINGS[max(RATINGS.index(rating), RATINGS.index(ceiling))]


def decide(reports: Sequence[Report]) -> Decision:
    available = [r for r in reports if r.available]
    total = sum(WEIGHTS.get(r.analyst, 0.0) for r in available)
    score = sum(WEIGHTS.get(r.analyst, 0.0) * r.score for r in available) / total if total else 0.0
    rating = rating_for(score)

    caps: list[str] = []
    if len(available) < MIN_ANALYSTS:
        rating = _cap(rating, "Hold")
        caps.append(f"Only {len(available)} of {len(reports)} analysts had data: at most Hold")
    flags = set().union(*(r.flags for r in available)) if available else set()
    for flag, (ceiling, why) in CAPS.items():
        if flag in flags:
            rating = _cap(rating, ceiling)
            caps.append(f"{why}: at most {ceiling}")

    sign = (score > 0) - (score < 0)
    agree = sum(
        WEIGHTS.get(r.analyst, 0.0)
        for r in available
        if sign and (r.score > 0) - (r.score < 0) == sign
    )
    bull = tuple(f"{r.analyst}: {t}" for r in available for w, t in r.points if w > 0)
    bear = tuple(f"{r.analyst}: {t}" for r in available for w, t in r.points if w < 0)
    return Decision(
        rating=rating,
        score=score,
        confidence=agree / total if total else 0.0,
        bull=bull,
        bear=bear,
        caps=tuple(caps),
        reports=tuple(reports),
    )
