# Author: Kai0421
"""Council of Ricks (bot.ricks): three deterministic voters and a tally. No I/O."""

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot import ricks as c
from bot.sentiment import Snapshot

SRC = Path(__file__).parents[1] / "src" / "bot"


def snap(score=80, vol=70, tagged=5, net=0.6):
    return Snapshot(ts="2026-10-12T14:00:00Z", symbol="NVDA", price=100.0, score=score,
                    volume_score=vol, post_net=net, post_tagged=tagged)  # fmt: skip


def bars(n=260, step=0.002, last_jump=0.0, volume=2_000_000, last_volume=None):
    close = 100 * np.cumprod(np.full(n, 1 + step))
    close[-1] *= 1 + last_jump
    vol = np.full(n, float(volume))
    if last_volume:
        vol[-1] = last_volume
    idx = pd.bdate_range(end="2026-10-09", periods=n)
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995,
                         "close": close, "volume": vol}, index=idx)  # fmt: skip


def ctx(**kw):
    base = dict(spy_above_sma200=True, move_atr=0.5, earnings_in_days=30,
                avg_dollar_volume=500e6, sector_open=0, sector_cap=2)  # fmt: skip
    return c.RiskContext(**{**base, **kw})


# ---------------------------------------------------------------- vote basics
def test_a_degree_is_clipped_and_maps_to_a_stance():
    assert c.Vote("x", 5.0, "").degree == 1.0 and c.Vote("x", -5.0, "").degree == -1.0
    assert c.Vote("x", 0.3, "").stance == "for"
    assert c.Vote("x", -0.3, "").stance == "against"
    assert c.Vote("x", 0.29, "").stance == "abstain"


# ---------------------------------------------------------------------- tally
def v(*degrees):
    return [c.Vote(f"m{i}", d, "") for i, d in enumerate(degrees)]


def test_two_for_one_abstain_buys():
    assert c.tally(v(0.6, 0.5, 0.0)).buy is True


def test_one_for_two_abstain_does_not_buy_and_ties_cannot_buy():
    assert c.tally(v(0.9, 0.0, 0.0)).buy is False
    assert c.tally(v(0.9, -0.4, 0.0)).buy is False  # 1 for, 1 against, 1 abstain


def test_a_strong_objection_vetoes_even_with_two_votes_for():
    out = c.tally(v(0.9, 0.9, -0.7))
    assert out.buy is False and "veto" in out.reason


def test_a_mild_objection_does_not_veto():
    assert c.tally(v(0.9, 0.9, -0.4)).buy is True


def test_the_mean_degree_must_clear_the_bar():
    assert c.tally(v(0.31, 0.31, -0.69)).buy is False  # 2 for, no veto, mean < 0.3


def test_the_tally_needs_exactly_the_members_it_was_built_for():
    with pytest.raises(ValueError):
        c.tally(v(0.9, 0.9))


def test_the_verdict_records_every_vote_and_the_mean():
    out = c.tally(v(0.6, 0.3, 0.0))
    assert [x.degree for x in out.votes] == [0.6, 0.3, 0.0]
    assert out.mean == pytest.approx(0.3)


# ---------------------------------------------------------------- member 1
def test_crowd_for_a_bullish_busy_well_sampled_stock():
    assert c.crowd_vote(snap()).stance == "for"


def test_crowd_degree_grows_with_the_score_and_the_volume():
    assert c.crowd_vote(snap(score=90)).degree > c.crowd_vote(snap(score=75)).degree
    assert c.crowd_vote(snap(vol=90)).degree > c.crowd_vote(snap(vol=45)).degree


def test_crowd_is_against_a_bearish_stock():
    assert c.crowd_vote(snap(score=15, net=-0.6)).stance == "against"


def test_crowd_votes_on_score_and_volume_alone_when_posts_are_missing_or_thin():
    for s in (snap(tagged=0, net=None), snap(tagged=1)):
        vote = c.crowd_vote(s)
        assert vote.stance == "for" and "no post data" in vote.reason
    assert c.crowd_vote(snap(score=15, tagged=0, net=None)).stance == "against"
    assert c.crowd_vote(snap(score=50, tagged=0, net=None)).stance == "abstain"
    # thin posts must not drag the vote: the extreme net is ignored
    assert (
        c.crowd_vote(snap(tagged=1, net=-1.0)).degree
        == c.crowd_vote(snap(tagged=0, net=None)).degree
    )


@pytest.mark.parametrize("s", [None, snap(vol=None)])
def test_crowd_abstains_without_enough_data(s):
    vote = c.crowd_vote(s)
    assert vote.stance == "abstain" and vote.degree == 0.0


# ---------------------------------------------------------------- member 2
def test_tape_inputs_from_bars():
    t = c.tape_inputs(bars())
    assert t is not None and t.close > t.sma50 > t.sma200 and t.ret20 > 0


def test_tape_needs_enough_history():
    assert c.tape_inputs(bars(n=100)) is None


def test_tape_for_an_uptrend_confirmed_by_volume():
    t = c.tape_inputs(bars(last_volume=6_000_000))
    assert c.tape_vote(t).stance == "for"


def test_tape_against_a_downtrend():
    t = c.tape_inputs(bars(step=-0.002))
    assert c.tape_vote(t).stance == "against"


def test_tape_abstains_without_inputs():
    assert c.tape_vote(None).stance == "abstain"


# ---------------------------------------------------------------- member 3
def test_skeptic_is_for_when_nothing_is_wrong():
    assert c.skeptic_vote(ctx(), crowd=0.5, tape=0.5).stance == "for"


@pytest.mark.parametrize("kw", [
    {"spy_above_sma200": False}, {"move_atr": 3.0}, {"earnings_in_days": 2},
    {"avg_dollar_volume": 5e6}, {"sector_open": 2},
])  # fmt: skip
def test_each_red_flag_pulls_the_skeptic_off_for(kw):
    assert c.skeptic_vote(ctx(**kw), crowd=0.5, tape=0.5).stance != "for"


def test_hype_without_a_price_response_is_a_red_flag():
    quiet = c.skeptic_vote(ctx(), crowd=0.8, tape=0.0)
    confirmed = c.skeptic_vote(ctx(), crowd=0.8, tape=0.6)
    assert quiet.degree < confirmed.degree


def test_unknown_facts_make_the_skeptic_more_careful_not_less():
    known = c.skeptic_vote(ctx(), crowd=0.5, tape=0.5)
    unknown = c.skeptic_vote(ctx(spy_above_sma200=None, earnings_in_days=None),
                             crowd=0.5, tape=0.5)  # fmt: skip
    assert unknown.degree < known.degree


def test_an_unknown_earnings_date_is_never_a_vote_for():
    unknown = c.skeptic_vote(ctx(earnings_in_days=None), crowd=0.5, tape=0.5)
    assert unknown.degree < c.CouncilRules().for_at


def test_several_red_flags_together_reach_the_veto_level():
    bad = ctx(spy_above_sma200=False, earnings_in_days=1, move_atr=3.5)
    assert c.skeptic_vote(bad, crowd=0.5, tape=0.5).degree <= c.CouncilRules().veto


# --------------------------------------------------------------- whole council
def test_ricks_buys_a_confirmed_bullish_stock():
    out = c.convene(snap(), bars(last_volume=6_000_000), ctx())
    assert out.buy is True and len(out.votes) == 3


def test_ricks_will_not_buy_hype_the_price_does_not_confirm():
    out = c.convene(snap(score=95, vol=95), bars(step=-0.002), ctx())
    assert out.buy is False


def test_ricks_will_not_buy_in_a_bear_market_even_if_crowd_and_tape_agree():
    out = c.convene(snap(), bars(last_volume=6_000_000),
                    ctx(spy_above_sma200=False, earnings_in_days=1, move_atr=3.5))  # fmt: skip
    assert out.buy is False


# ------------------------------------------------------------------- guards
def test_the_council_module_is_pure():
    tree = ast.parse((SRC / "ricks.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
    banned = {"bot.broker", "bot.execute", "bot.journal", "bot.gather", "bot.notify", "bot.data",
              "requests", "subprocess", "os"}  # fmt: skip
    assert not mods & banned
    assert (SRC / "ricks.py").read_text(encoding="utf-8").count("open(") == 0
