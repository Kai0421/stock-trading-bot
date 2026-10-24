# Author: Kai0421
"""Council shadow log: convene the Ricks on data the bot already holds, record what they would
do, label what happened afterwards, and judge them. Pure functions over DataFrames, no broker,
no network (the runner script does the I/O). Nothing here can place an order.

- ``risk_context`` builds Skeptic Rick's inputs from daily bars.
- ``convene_day`` returns one log row per stock for a day.
- ``label`` fills the realised returns once the future bars exist (entry at the next open).
- ``add_excess`` / ``summary`` compare buys with the average stock of the same day and show what
  each Rick is worth on his own.
- ``demotion`` is the self-correction rule for a future capped paper trial: it can only switch
  the council off, never on (docs/21-sentiment-council.md).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from bot.indicators import atr, sma
from bot.ricks import DEFAULT_RULES, CouncilRules, RiskContext, convene
from bot.sentiment import Snapshot
from bot.slack_format import bullets, section, table

HORIZONS = (1, 5)
RICKS = ("crowd", "tape", "skeptic")


def risk_context(
    bars: pd.DataFrame,
    spy: pd.DataFrame | None,
    sector_open: int = 0,
    sector_cap: int = 3,
    earnings_in_days: int | None = None,
) -> RiskContext:
    """Skeptic Rick's inputs. Anything that cannot be computed stays None (counts against)."""
    above: bool | None = None
    if spy is not None and len(spy) >= 200:
        s200 = sma(spy["close"], 200).iloc[-1]
        if not pd.isna(s200):
            above = bool(spy["close"].iloc[-1] > s200)
    move: float | None = None
    if len(bars) >= 20:
        a = atr(bars["high"], bars["low"], bars["close"], 14).iloc[-1]
        if not pd.isna(a) and a > 0:
            move = float((bars["close"].iloc[-1] - bars["close"].iloc[-2]) / a)
    dollar: float | None = None
    if len(bars) >= 20:
        dollar = float((bars["close"] * bars["volume"]).iloc[-20:].mean())
    return RiskContext(
        spy_above_sma200=above,
        move_atr=move,
        earnings_in_days=earnings_in_days,
        avg_dollar_volume=dollar,
        sector_open=sector_open,
        sector_cap=sector_cap,
    )


def convene_day(
    day: pd.Timestamp,
    snaps: Mapping[str, Snapshot],
    bars: Mapping[str, pd.DataFrame],
    spy: pd.DataFrame | None,
    sectors: Mapping[str, str],
    held: Iterable[str] = (),
    sector_cap: int = 3,
    rules: CouncilRules = DEFAULT_RULES,
    news: Mapping[str, Mapping] | None = None,
) -> list[dict]:
    """One row per stock with bars up to ``day``. A missing snapshot makes Crowd Rick abstain.

    ``news`` (bot.newsfeat.features) adds log-only columns; it never changes a vote.
    """
    held_by_sector: dict[str, int] = {}
    for h in held:
        sec = sectors.get(h)
        if sec is not None:
            held_by_sector[sec] = held_by_sector.get(sec, 0) + 1
    spy_cut = spy.loc[:day] if spy is not None else None
    rows = []
    for symbol, sec in sectors.items():
        full = bars.get(symbol)
        if full is None:
            continue
        cut = full.loc[:day]
        if cut.empty:
            continue
        ctx = risk_context(cut, spy_cut, held_by_sector.get(sec, 0), sector_cap)
        verdict = convene(snaps.get(symbol), cut, ctx, rules)
        votes = {v.member: v for v in verdict.votes}
        rows.append(
            {
                "day": pd.Timestamp(day),
                "symbol": symbol,
                "crowd": votes["crowd"].degree,
                "tape": votes["tape"].degree,
                "skeptic": votes["skeptic"].degree,
                "mean": verdict.mean,
                "buy": verdict.buy,
                "reason": f"{verdict.reason} | skeptic: {votes['skeptic'].reason}",
                "close": float(cut["close"].iloc[-1]),
                **(dict(news[symbol]) if news and symbol in news else {}),
            }
        )
    return rows


def label(log: pd.DataFrame, bars: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Realised returns: enter at the next open, exit at the close ``h`` sessions later.

    Recomputed from the bars each call, so it is safe to repeat; rows whose future bars do not
    exist yet stay NaN.
    """
    out = log.copy()
    out["day"] = pd.to_datetime(out["day"])
    for h in HORIZONS:
        out[f"ret{h}"] = np.nan
    for i, row in out.iterrows():
        b = bars.get(row["symbol"])
        if b is None or row["day"] not in b.index:
            continue
        pos = b.index.get_loc(row["day"])
        if pos + 1 >= len(b):
            continue
        entry = float(b["open"].iloc[pos + 1])
        for h in HORIZONS:
            if pos + h < len(b):
                out.at[i, f"ret{h}"] = float(b["close"].iloc[pos + h]) / entry - 1
    return out


def add_excess(df: pd.DataFrame) -> pd.DataFrame:
    """Subtract that day's average return so a rising market does not look like skill."""
    out = df.copy()
    for h in HORIZONS:
        out[f"x{h}"] = out[f"ret{h}"] - out.groupby("day")[f"ret{h}"].transform("mean")
    return out


def _stats(sel: pd.DataFrame) -> dict:
    sel = sel.dropna(subset=["x5"]) if "x5" in sel else sel.iloc[0:0]
    if sel.empty:
        return {"n": 0, "mean_x1_bps": None, "mean_x5_bps": None, "hit5": None}
    return {
        "n": int(len(sel)),
        "mean_x1_bps": round(float(sel["x1"].mean()) * 1e4, 1),
        "mean_x5_bps": round(float(sel["x5"].mean()) * 1e4, 1),
        "hit5": round(float((sel["ret5"] > 0).mean()), 3),
    }


def summary(df: pd.DataFrame, rules: CouncilRules = DEFAULT_RULES) -> dict:
    """Buys against all rows, and each Rick's own 'for' votes (the ablation view)."""
    labelled = df.dropna(subset=["ret5"]) if "ret5" in df else df
    return {
        "all": _stats(labelled),
        "buys": _stats(labelled[labelled["buy"].astype(bool)]),
        "by_rick": {r: _stats(labelled[labelled[r] >= rules.for_at]) for r in RICKS},
    }


NAMES = {"crowd": "Crowd Rick", "tape": "Rick C127", "skeptic": "Skeptic Rick"}


def _stance(deg: float, rules: CouncilRules = DEFAULT_RULES) -> str:
    if deg <= rules.veto:
        return "vetoes it"
    if deg <= rules.against_at:
        return "is against"
    if deg >= rules.for_at:
        return "likes it"
    return "is unsure"


def _why_not(r: Mapping, rules: CouncilRules = DEFAULT_RULES) -> str:
    if r["crowd"] < rules.for_at:
        return "the crowd isn't behind it"
    vetoes = [NAMES[k] for k in RICKS if r[k] <= rules.veto]
    if vetoes:
        return f"{' and '.join(vetoes)} vetoed it"
    if sum(r[k] >= rules.for_at for k in RICKS) < rules.min_for:
        return "not enough Ricks like it"
    return "the combined score is too low"


def _news_note(r: Mapping) -> str:
    n = int(r.get("news_n") or 0)
    if not n:
        return ""
    risk = int(r.get("news_risk_n") or 0)
    word = "headline" if n == 1 else "headlines"
    return f" {n} {word}" + (f", {risk} with a warning word." if risk else ".")


def digest(rows: Sequence[Mapping], top: int = 5) -> tuple[str, str]:
    """(title, text) for the Slack post, in plain English. Never an order."""
    day = pd.Timestamp(rows[0]["day"]) if rows else None
    when = f"{day:%a %d %b}" if day is not None else "today"
    buys = sorted((r for r in rows if r["buy"]), key=lambda r: -r["mean"])

    def votes(r: Mapping) -> list[str]:
        return [f"{_stance(r[k])} ({r[k]:+.1f})" for k in RICKS]

    if buys:
        head = f"The council looked at {len(rows)} stocks and would have bought {len(buys)}"
        shown = [(r, "would buy") for r in buys[:top]]
        title_rows = "Would buy"
    else:
        near = sorted(rows, key=lambda r: -r["mean"])[:3]
        head = f"The council looked at {len(rows)} stocks and would not have bought any"
        shown = [(r, f"no, {_why_not(r)}") for r in near]
        title_rows = "Closest calls"
    header = ["Stock", *[NAMES[k] for k in RICKS], "Decision"]
    table_rows = [[str(r["symbol"]), *votes(r), call] for r, call in shown]
    notes = [f"{r['symbol']}:{_news_note(r)}" for r, _ in shown if _news_note(r)]
    flagged = [r["symbol"] for r in rows if r.get("news_risk_n")]
    blocks = [bullets([head]), section(title_rows, table(header, table_rows))]
    if notes:
        blocks.append(section("Headlines", bullets(notes)))
    if flagged:
        blocks.append(
            section("Stocks with a warning-style headline", bullets([", ".join(flagged[:10])]))
        )
    blocks.append("This is a practice run. No orders were placed and the real bot ignores it.")
    return f"Council of Ricks: practice run for {when}", "\n\n".join(b for b in blocks if b)


def news_summary(df: pd.DataFrame) -> dict:
    """Do stocks with risk headlines do worse overnight than the rest? (logged, not acted on)"""
    if "news_n" not in df:
        return {}
    d = df.dropna(subset=["news_n", "ret5"]) if "ret5" in df else df.iloc[0:0]
    if d.empty:
        return {}
    return {
        "risk_headline": _stats(d[d["news_risk_n"] > 0]),
        "earnings_headline": _stats(d[d["news_earn_n"] > 0]),
        "other_news": _stats(d[(d["news_risk_n"] == 0) & (d["news_n"] > 0)]),
        "no_news": _stats(d[d["news_n"] == 0]),
    }


# ------------------------------------------------------------------- self-correction


@dataclass(frozen=True)
class TrialRules:
    min_trades_for_mean: int = 20  # average-R test needs this many closed trades
    max_losing_streak: int = 5
    max_drawdown_r: float = 5.0  # peak-to-trough of cumulative R


DEFAULT_TRIAL = TrialRules()


@dataclass(frozen=True)
class Demotion:
    demote: bool
    reason: str = ""


def demotion(
    results_r: Sequence[float], rules: TrialRules = DEFAULT_TRIAL, data_fresh: bool = True
) -> Demotion:
    """Should the council drop back to shadow-only? It can only turn the council off.

    ``results_r`` are closed trades in R (multiples of the risk taken), oldest first.
    """
    if not data_fresh:
        return Demotion(True, "sentiment data stale or missing")
    streak = longest = 0
    for r in results_r:
        streak = streak + 1 if r < 0 else 0
        longest = max(longest, streak)
    if longest >= rules.max_losing_streak:
        return Demotion(True, f"losing streak of {longest}")
    cum = np.cumsum(results_r) if len(results_r) else np.array([0.0])
    drawdown = float((np.maximum.accumulate(np.maximum(cum, 0)) - cum).max())
    if drawdown >= rules.max_drawdown_r:
        return Demotion(True, f"drawdown of {drawdown:.1f}R")
    if len(results_r) >= rules.min_trades_for_mean and float(np.mean(results_r)) < 0:
        return Demotion(True, f"average {np.mean(results_r):+.2f}R over {len(results_r)} trades")
    return Demotion(False)
