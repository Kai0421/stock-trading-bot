# Author: Kai0421
"""Rule-based analysts. Each turns one data source into a ``Report``: a score from -1
(bearish) to +1 (bullish), the points behind it, and risk flags for the judge.

All inputs are point in time for a premarket view of ``as_of``: bars dated before
``as_of``, filings filed before it, headlines published before 09:00 ET on it.
Missing data makes an analyst unavailable; it never guesses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np
import pandas as pd

from bot.altdata.news import is_major
from bot.desk.facts import AnnualFacts
from bot.desk.judge import Report
from bot.indicators import atr, rsi, sma

MIN_BARS = 253  # a year of sessions for the 52-week high, and the 200-day SMA
MIN_MARKET_BARS = 200
HIGH_VOLATILITY_ATR = 0.04

POSITIVE = re.compile(
    r"\b(beats?|tops|upgrades?|upgraded|raise[sd]? (its |full[- ]year )?(guidance|outlook|"
    r"forecast|dividend)|approv(al|ed|es)|buybacks?|repurchases?|outperform|surge[sd]?|"
    r"jump(s|ed)?|rall(y|ies|ied))\b",
    re.IGNORECASE,
)
NEGATIVE = re.compile(
    r"\b(miss(es|ed)?|downgrades?|downgraded|lower(s|ed)? (its )?(guidance|outlook|forecast)|"
    r"warns?|warning|layoffs?|plunge[sd]?|falls?|fell|slump(s|ed)?|underperform|weak(er)?|"
    r"decline[sd]?)\b",
    re.IGNORECASE,
)


def _report(name: str, points: list[tuple[float, str]], flags: set[str] | None = None) -> Report:
    score = float(np.clip(sum(w for w, _ in points), -1.0, 1.0))
    return Report(name, score, tuple(points), flags=frozenset(flags or ()))


def _unavailable(name: str, why: str) -> Report:
    return Report(name, 0.0, available=False, note=why)


def _before(bars: pd.DataFrame | None, as_of: date) -> pd.DataFrame:
    if bars is None:
        return pd.DataFrame()
    return bars.loc[bars.index < pd.Timestamp(as_of)]


def technical_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """The technical analyst for every bar at once: inputs, rule contributions and
    ``tech_score`` (NaN until there is enough history). Row ``i`` uses bars ``<= i``."""
    close = bars["close"].astype("float64")
    f = pd.DataFrame(index=bars.index)
    f["close"] = close
    f["sma50"] = sma(close, 50)
    f["sma200"] = sma(close, 200)
    f["rsi14"] = rsi(close, 14)
    f["atr_pct"] = atr(bars["high"], bars["low"], close, 14) / close
    f["ret126"] = close / close.shift(126) - 1
    f["drawdown"] = close / close.rolling(252, min_periods=252).max() - 1
    known = f.notna().all(axis=1)
    above = close > f["sma200"]
    f["p_trend"] = np.where(above, 0.3, -0.3)
    f["p_cross"] = np.where(f["sma50"] > f["sma200"], 0.2, -0.2)
    f["p_momentum"] = np.select([f["ret126"] > 0.10, f["ret126"] < -0.10], [0.2, -0.2], 0.0)
    f["p_rsi"] = np.select([f["rsi14"] > 75, (f["rsi14"] < 30) & above], [-0.15, 0.1], 0.0)
    f["p_drawdown"] = np.where(f["drawdown"] < -0.20, -0.2, 0.0)
    f["high_volatility"] = f["atr_pct"] > HIGH_VOLATILITY_ATR
    f["p_volatility"] = np.where(f["high_volatility"], -0.15, 0.0)
    points = f[[c for c in f.columns if c.startswith("p_")]].sum(axis=1)
    f["tech_score"] = points.clip(-1.0, 1.0).where(known)
    return f


def market_frame(spy_bars: pd.DataFrame) -> pd.DataFrame:
    """The market analyst for every SPY bar: ``mkt_score`` and ``market_downtrend``."""
    close = spy_bars["close"].astype("float64")
    f = pd.DataFrame(index=spy_bars.index)
    f["close"] = close
    f["sma50"] = sma(close, 50)
    f["sma200"] = sma(close, 200)
    f["ret63"] = close / close.shift(63) - 1
    known = f.notna().all(axis=1)
    f["market_downtrend"] = (close <= f["sma200"]) & known
    f["p_trend"] = np.where(f["market_downtrend"], -0.4, 0.4)
    f["p_cross"] = np.where(f["sma50"] > f["sma200"], 0.2, -0.2)
    f["p_momentum"] = np.where(f["ret63"] > 0, 0.2, -0.2)
    points = f["p_trend"] + f["p_cross"] + f["p_momentum"]
    f["mkt_score"] = points.clip(-1.0, 1.0).where(known)
    return f


def technical(bars: pd.DataFrame | None, as_of: date) -> Report:
    b = _before(bars, as_of)
    if len(b) < MIN_BARS:
        return _unavailable("technical", f"needs {MIN_BARS} daily bars, has {len(b)}")
    r = technical_frame(b).iloc[-1]
    if pd.isna(r["tech_score"]):
        return _unavailable("technical", "not enough history for the indicators")
    above = r["p_trend"] > 0
    points = [
        (r["p_trend"], f"Close is {'above' if above else 'below'} the 200-day SMA"),
        (r["p_cross"], "50-day SMA is above the 200-day (uptrend)" if r["p_cross"] > 0
         else "50-day SMA is below the 200-day (downtrend)"),
    ]  # fmt: skip
    if r["p_momentum"]:
        move = "Up" if r["p_momentum"] > 0 else "Down"
        points.append((r["p_momentum"], f"{move} {abs(r['ret126']):.0%} over 6 months"))
    if r["p_rsi"] < 0:
        points.append((r["p_rsi"], f"RSI(14) {r['rsi14']:.0f}: overbought"))
    elif r["p_rsi"] > 0:
        points.append((r["p_rsi"], f"RSI(14) {r['rsi14']:.0f}: oversold inside an uptrend"))
    if r["p_drawdown"]:
        points.append((r["p_drawdown"], f"{-r['drawdown']:.0%} below its 52-week high"))
    if r["p_volatility"]:
        points.append((r["p_volatility"], f"ATR(14) is {r['atr_pct']:.1%} of price: volatile"))
    flags = {"high_volatility"} if r["high_volatility"] else set()
    return Report(
        "technical", float(r["tech_score"]), tuple((float(w), t) for w, t in points),
        flags=frozenset(flags),
    )  # fmt: skip


def market(spy_bars: pd.DataFrame | None, as_of: date) -> Report:
    b = _before(spy_bars, as_of)
    if len(b) < MIN_MARKET_BARS:
        return _unavailable("market", f"needs {MIN_MARKET_BARS} SPY bars, has {len(b)}")
    r = market_frame(b).iloc[-1]
    if pd.isna(r["mkt_score"]):
        return _unavailable("market", "not enough SPY history for the indicators")
    move = "up" if r["p_momentum"] > 0 else "down"
    points = [
        (r["p_trend"], "SPY is below its 200-day SMA" if r["market_downtrend"]
         else "SPY is above its 200-day SMA"),
        (r["p_cross"], f"SPY 50-day is {'above' if r['p_cross'] > 0 else 'below'} its 200-day"),
        (r["p_momentum"], f"SPY {move} {abs(r['ret63']):.0%} over 3 months"),
    ]  # fmt: skip
    flags = {"market_downtrend"} if r["market_downtrend"] else set()
    return Report(
        "market", float(r["mkt_score"]), tuple((float(w), t) for w, t in points),
        flags=frozenset(flags),
    )  # fmt: skip


def fundamentals(facts: AnnualFacts | None) -> Report:
    if facts is None or len(facts.revenue) < 2:
        return _unavailable("fundamentals", "fewer than two annual revenue figures")
    (end0, rev0), (_, rev1) = facts.revenue[0], facts.revenue[1]
    points: list[tuple[float, str]] = []
    if rev1 > 0:
        growth = rev0 / rev1 - 1
        if growth > 0.10:
            points.append((0.3, f"Revenue grew {growth:.0%} in fiscal {end0.year}"))
        elif growth >= 0:
            points.append((0.1, f"Revenue grew {growth:.0%} in fiscal {end0.year}"))
        else:
            points.append((-0.3, f"Revenue fell {-growth:.0%} in fiscal {end0.year}"))
    income = dict(facts.net_income)
    if end0 in income and rev0 > 0:
        margin = income[end0] / rev0
        if margin > 0.15:
            points.append((0.2, f"Net margin {margin:.0%}"))
        elif margin > 0:
            points.append((0.1, f"Net margin {margin:.0%}"))
        else:
            points.append((-0.4, "Net loss in the last fiscal year"))
    if len(facts.eps) >= 2 and facts.eps[1][1] > 0:
        eps_growth = facts.eps[0][1] / facts.eps[1][1] - 1
        if eps_growth > 0.10:
            points.append((0.2, f"Diluted EPS grew {eps_growth:.0%}"))
        elif eps_growth < 0:
            points.append((-0.2, f"Diluted EPS fell {-eps_growth:.0%}"))
    return _report("fundamentals", points)


INSIDER_POINTS = {
    "insider_cluster_buy_90d": (0.5, "Several insiders bought on the open market (90 days)"),
    "insider_ceo_cfo_buy_30d": (0.3, "The CEO or CFO bought on the open market (30 days)"),
    "activist_13d_60d": (0.2, "An activist filed a 13D (60 days)"),
    "insider_heavy_selling_30d": (-0.4, "Officers sold heavily outside 10b5-1 plans (30 days)"),
}


def insiders(flags: Mapping[str, bool] | None) -> Report:
    if flags is None:
        return _unavailable("insiders", "no EDGAR data (set SEC_USER_AGENT)")
    points = [p for key, p in INSIDER_POINTS.items() if flags.get(key)]
    if not points:
        return Report("insiders", 0.0, ((0.0, "No notable insider activity"),))
    return _report("insiders", points)


def news(headlines: Sequence[str] | None) -> Report:
    if headlines is None:
        return _unavailable("news", "no news data (needs Alpaca keys)")
    points: list[tuple[float, str]] = []
    flags: set[str] = set()
    shocks = [h for h in headlines if is_major(h)]
    if shocks:
        points.append((-0.6, f"Major news: {shocks[0]}"))
        flags.add("news_shock")
    pos = neg = 0
    for h in headlines:
        tone = len(POSITIVE.findall(h)) - len(NEGATIVE.findall(h))
        pos += tone > 0
        neg += tone < 0
    if pos + neg:
        weight = 0.5 * (pos - neg) / (pos + neg) * min(1.0, (pos + neg) / 3)
        text = f"{pos} positive and {neg} negative of {len(headlines)} headlines"
        points.append((weight, text))
    if not points:
        return Report("news", 0.0, ((0.0, f"{len(headlines)} headlines, none decisive"),))
    return _report("news", points, flags)
