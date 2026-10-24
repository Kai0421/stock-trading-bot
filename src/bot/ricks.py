# Author: Kai0421
"""Council of Ricks: three deterministic voters decide whether a stock becomes a buy candidate.

Pure functions, no I/O, no broker: the bot's own code convenes the council on data it already
holds, and a buy verdict is only a candidate that goes through the same evidence gate, stop,
cash check and sizing caps as every other setup. Claude never votes and never orders; a model
may read text in the collector and write numbers to a file, nothing more (AGENTS.md).

Each Rick returns a degree from -1 (strongly against) to +1 (strongly for):

- **Crowd Rick** reads Stocktwits: score, message-volume surge, and the tagged-post net when
  enough posts were sampled.
- **Rick C127** reads the stock's own daily bars: trend, momentum, volume confirmation.
- **Skeptic Rick** looks for reasons not to buy: weak market, stock already extended,
  earnings close, thin liquidity, full sector, and hype the price does not confirm. Unknown
  facts count against, so missing data makes him more careful, never less.

A buy needs at least two Ricks for, no Rick at or below the veto level, and a mean degree
above the bar. An abstention is not a vote for, so a tie cannot buy. The thresholds are
hypotheses to be tested on the shadow log (docs/21-sentiment-council.md), not tuned values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pandas as pd

from bot.indicators import sma
from bot.sentiment import Snapshot


@dataclass(frozen=True)
class CouncilRules:
    members: int = 3
    for_at: float = 0.3  # a degree at or above this is a vote for
    against_at: float = -0.3  # at or below this, a vote against
    veto: float = -0.7  # any Rick at or below this blocks the buy
    min_for: int = 2
    min_mean: float = 0.3
    crowd_min_tagged: int = 3
    big_move_atr: float = 2.5  # already moved this many ATRs today: extended
    earnings_days: int = 5  # earnings this close: avoid
    min_dollar_volume: float = 20e6
    require_crowd: bool = True  # a sentiment council: the tape and skeptic alone cannot buy


DEFAULT_RULES = CouncilRules()


@dataclass(frozen=True)
class Vote:
    member: str
    degree: float
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "degree", max(-1.0, min(1.0, float(self.degree))))

    @property
    def stance(self) -> str:
        if self.degree >= DEFAULT_RULES.for_at:
            return "for"
        if self.degree <= DEFAULT_RULES.against_at:
            return "against"
        return "abstain"


@dataclass(frozen=True)
class Verdict:
    buy: bool
    mean: float
    reason: str
    votes: tuple[Vote, ...] = field(default_factory=tuple)


def tally(votes: Sequence[Vote], rules: CouncilRules = DEFAULT_RULES) -> Verdict:
    if len(votes) != rules.members:
        raise ValueError(f"the council has {rules.members} members, got {len(votes)} votes")
    mean = sum(v.degree for v in votes) / len(votes)
    n_for = sum(v.degree >= rules.for_at for v in votes)
    vetoes = [v.member for v in votes if v.degree <= rules.veto]
    if vetoes:
        reason = f"veto by {', '.join(vetoes)}"
    elif n_for < rules.min_for:
        reason = f"only {n_for} of {len(votes)} for"
    elif mean < rules.min_mean:
        reason = f"mean {mean:+.2f} below {rules.min_mean:+.2f}"
    else:
        return Verdict(True, mean, f"{n_for} of {len(votes)} for, mean {mean:+.2f}", tuple(votes))
    return Verdict(False, mean, reason, tuple(votes))


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ Crowd Rick


def crowd_vote(snap: Snapshot | None, rules: CouncilRules = DEFAULT_RULES) -> Vote:
    """Score and message volume carry the vote. The tagged-post net adds to it only when enough
    posts were sampled; the unattended collector usually skips posts, and without this fallback
    Crowd Rick would abstain on every stock."""
    if snap is None or snap.volume_score is None:
        return Vote("crowd", 0.0, "not enough Stocktwits data")
    base = (snap.score - 50) / 50  # the score is normalised to the stock's own history
    busy = 0.5 + 0.5 * _clip((snap.volume_score - 40) / 40, 0.0, 1.0)  # quiet talk counts less
    why = f"score {snap.score:.0f}, volume {snap.volume_score:.0f}"
    if snap.post_net is not None and snap.post_tagged >= rules.crowd_min_tagged:
        degree = 0.7 * base * busy + 0.3 * snap.post_net
        why += f", posts {snap.post_net:+.2f}"
    else:
        degree = base * busy
        why += ", no post data"
    return Vote("crowd", _clip(degree, -1.0, 1.0), why)


# ------------------------------------------------------------------- Rick C127


@dataclass(frozen=True)
class TapeInputs:
    close: float
    sma50: float
    sma200: float
    ret5: float
    ret20: float
    rel_volume: float  # last day's volume over the mean of the 20 days before


def tape_inputs(bars: pd.DataFrame | None) -> TapeInputs | None:
    if bars is None or len(bars) < 210:
        return None
    close, volume = bars["close"], bars["volume"]
    s50, s200 = sma(close, 50).iloc[-1], sma(close, 200).iloc[-1]
    base_volume = volume.iloc[-21:-1].mean()
    if pd.isna(s50) or pd.isna(s200) or not base_volume:
        return None
    last = float(close.iloc[-1])
    return TapeInputs(
        close=last,
        sma50=float(s50),
        sma200=float(s200),
        ret5=last / float(close.iloc[-6]) - 1,
        ret20=last / float(close.iloc[-21]) - 1,
        rel_volume=float(volume.iloc[-1] / base_volume),
    )


def tape_vote(t: TapeInputs | None) -> Vote:
    if t is None:
        return Vote("tape", 0.0, "not enough price history")
    degree = 0.4 if t.close > t.sma50 else -0.4
    degree += 0.2 if t.close > t.sma200 else -0.2
    degree += 0.2 * _clip(t.ret20 / 0.10)
    if t.rel_volume >= 1.5 and t.ret5 != 0:  # volume backs the recent direction
        degree += 0.2 if t.ret5 > 0 else -0.2
    side = "above" if t.close > t.sma50 else "below"
    return Vote("tape", degree, f"20-day {t.ret20:+.1%}, volume x{t.rel_volume:.1f}, {side} SMA50")


# ----------------------------------------------------------------- Skeptic Rick


@dataclass(frozen=True)
class RiskContext:
    spy_above_sma200: bool | None  # None = unknown
    move_atr: float | None  # today's move from the previous close, in daily ATRs
    earnings_in_days: int | None
    avg_dollar_volume: float | None
    sector_open: int  # positions already open in this stock's sector
    sector_cap: int


def skeptic_vote(
    ctx: RiskContext, crowd: float, tape: float, rules: CouncilRules = DEFAULT_RULES
) -> Vote:
    degree, flags = 0.5, []  # starts mildly for: nothing wrong is not a reason to buy

    def flag(penalty: float, why: str) -> None:
        nonlocal degree
        degree -= penalty
        flags.append(why)

    if ctx.spy_above_sma200 is None:
        flag(0.2, "market trend unknown")
    elif not ctx.spy_above_sma200:
        flag(0.6, "market below its 200-day average")
    if ctx.move_atr is not None and ctx.move_atr > rules.big_move_atr:
        flag(0.5, f"already up {ctx.move_atr:.1f} ATR")
    if ctx.earnings_in_days is None:
        flag(0.5, "earnings date unknown")  # v2: was 0.2, which left a free "for" on every stock
    elif ctx.earnings_in_days <= rules.earnings_days:
        flag(0.6, f"earnings in {ctx.earnings_in_days} days")
    if ctx.avg_dollar_volume is None or ctx.avg_dollar_volume < rules.min_dollar_volume:
        flag(0.5, "thin trading")
    if ctx.sector_open >= ctx.sector_cap:
        flag(0.4, "sector full")
    if crowd >= 0.5 and tape < 0.1:
        flag(0.6, "hype the price does not confirm")
    return Vote("skeptic", degree, "; ".join(flags) or "no red flags")


# ------------------------------------------------------------------- the council


def convene(
    snap: Snapshot | None,
    bars: pd.DataFrame | None,
    ctx: RiskContext,
    rules: CouncilRules = DEFAULT_RULES,
) -> Verdict:
    crowd = crowd_vote(snap, rules)
    tape = tape_vote(tape_inputs(bars))
    skeptic = skeptic_vote(ctx, crowd.degree, tape.degree, rules)
    verdict = tally([crowd, tape, skeptic], rules)
    if verdict.buy and rules.require_crowd and crowd.degree < rules.for_at:
        return Verdict(False, verdict.mean, "crowd is not for", verdict.votes)
    return verdict
