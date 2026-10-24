# Author: Kai0421
"""The bot's pass/fail test (docs/25-success-test.md). Read-only: reads the journal and SPY bars.

Written down before the results exist, so the verdict cannot be bent to fit the numbers.
Not imported by the order path.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from bot.risk import WILSON_Z_95, mean_r_lower_bound

MIN_TRADES = 100  # finished trades before a PASS or FAIL is allowed
EARLY_FAIL_MIN_TRADES = 30  # earliest point a clearly losing result may be called


@dataclass(frozen=True)
class Verdict:
    status: str  # INSUFFICIENT | PASS | FAIL | FAIL_EARLY
    trades: int
    mean_r: float
    r_lower: float
    r_upper: float
    win_rate: float
    net_pnl: float
    bot_return: float  # fraction, over the period the equity curve covers
    spy_same_exposure: float  # SPY return scaled by how invested the bot was each day
    reasons: tuple[str, ...]


def judge(
    r_multiples: list[float],
    pnls: list[float],
    bot_return: float,
    spy_same_exposure: float,
) -> Verdict:
    """PASS needs all three: n >= 100, 95% lower bound of mean R > 0, and a return above
    holding SPY with the same exposure. FAIL_EARLY: n >= 30 and the upper bound of mean R < 0.
    """
    n = len(r_multiples)
    mean_r = sum(r_multiples) / n if n else math.nan
    sd = math.sqrt(sum((r - mean_r) ** 2 for r in r_multiples) / (n - 1)) if n > 1 else math.nan
    lower = mean_r_lower_bound(mean_r, sd, n)
    upper = mean_r + WILSON_Z_95 * sd / math.sqrt(n) if n > 1 else math.nan
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / n if n else math.nan
    reasons: list[str] = []
    if n < EARLY_FAIL_MIN_TRADES or math.isnan(upper):
        status = "INSUFFICIENT"
        reasons.append(
            f"only {n} finished trade{'s' if n != 1 else ''}; need {MIN_TRADES} for a verdict"
        )
    elif upper < 0:
        status = "FAIL_EARLY"
        reasons.append("even the best case of the average trade is a loss")
    elif n < MIN_TRADES:
        status = "INSUFFICIENT"
        reasons.append(
            f"only {n} finished trade{'s' if n != 1 else ''}; need {MIN_TRADES} for a verdict"
        )
    else:
        ok_r = lower > 0
        ok_spy = bot_return > spy_same_exposure
        if not ok_r:
            reasons.append("the average trade is not clearly profitable (lower bound <= 0)")
        if not ok_spy:
            reasons.append("it did not beat holding SPY with the same exposure")
        status = "PASS" if ok_r and ok_spy else "FAIL"
    return Verdict(
        status, n, mean_r, lower, upper, win_rate, sum(pnls), bot_return, spy_same_exposure,
        tuple(reasons),
    )  # fmt: skip


def spy_same_exposure_return(equity: pd.DataFrame, spy_close: pd.Series) -> float:
    """SPY's return scaled each day by the bot's invested fraction at the previous close."""
    eq = equity.sort_values("day").reset_index(drop=True)
    spy = spy_close.copy()
    spy.index = pd.to_datetime(spy.index).strftime("%Y-%m-%d")
    total = 1.0
    for i in range(1, len(eq)):
        prev, cur = eq.loc[i - 1], eq.loc[i]
        p0, p1 = spy.get(prev["day"]), spy.get(cur["day"])
        if p0 is None or p1 is None or prev["equity"] <= 0:
            continue
        exposure = max(0.0, 1.0 - float(prev["cash"]) / float(prev["equity"]))
        total *= 1.0 + exposure * (float(p1) / float(p0) - 1.0)
    return total - 1.0


def evaluate(db: Path, spy_csv: Path, since: str) -> Verdict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    trades = pd.read_sql_query(
        "SELECT pnl, r_multiple FROM trades WHERE entry_date >= ? AND r_multiple IS NOT NULL",
        con, params=(since,),
    )  # fmt: skip
    equity = pd.read_sql_query(
        "SELECT day, equity, cash FROM equity WHERE day >= ? ORDER BY day", con, params=(since,)
    )
    con.close()
    spy = pd.read_csv(spy_csv, index_col="date")["close"]
    bot_ret = (
        float(equity["equity"].iloc[-1] / equity["equity"].iloc[0] - 1.0)
        if len(equity) > 1
        else 0.0
    )
    spy_ret = spy_same_exposure_return(equity, spy) if len(equity) > 1 else 0.0
    return judge(list(trades["r_multiple"]), list(trades["pnl"]), bot_ret, spy_ret)


def describe(v: Verdict) -> str:
    """Plain English, for the report and Slack."""
    head = {
        "INSUFFICIENT": "Too early to say",
        "PASS": "PASS: the bot has shown an edge",
        "FAIL": "FAIL: the bot has not shown an edge",
        "FAIL_EARLY": "FAIL (early): the bot is losing on average",
    }[v.status]
    lines = [
        head,
        f"Finished trades: {v.trades} (verdict needs {MIN_TRADES})",
        f"Average trade: {v.mean_r:+.2f} R (95% range {v.r_lower:+.2f} to {v.r_upper:+.2f})"
        if v.trades > 1
        else f"Average trade: {v.mean_r:+.2f} R",
        f"Win rate: {v.win_rate:.0%}; net profit: ${v.net_pnl:,.2f}",
        f"Account return {v.bot_return:+.2%} vs SPY with the same exposure "
        f"{v.spy_same_exposure:+.2%}",
    ]
    lines += [f"Why: {r}" for r in v.reasons]
    return "\n".join(lines)
