# Author: Kai0421
"""Is a research result luck or an edge? Pure helpers for the hypothesis test (docs/26).

Every research result should carry the average, its 95% range, the t-statistic and the p-value,
and should be read against how many ideas have been tried: test enough rules and some pass by
luck alone, so the bar rises with the count (Bonferroni: alpha divided by the number of ideas).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

SMALL_SAMPLE = 30  # below this the normal approximation is rough


@dataclass(frozen=True)
class MeanTest:
    n: int
    mean: float
    t: float  # mean divided by its standard error (nan when it cannot be computed)
    p: float  # two-sided chance of a result this far from zero if there were no edge
    low: float  # 95% range for the true average
    high: float
    small_sample: bool


def mean_test(values: Sequence[float]) -> MeanTest:
    """Test whether the average of ``values`` differs from zero (normal approximation)."""
    x = np.asarray([v for v in values if v is not None and not math.isnan(v)], dtype=float)
    n = len(x)
    if n == 0:
        return MeanTest(0, math.nan, math.nan, 1.0, math.nan, math.nan, True)
    mean = float(x.mean())
    if n < 2 or float(x.std(ddof=1)) == 0.0:
        return MeanTest(n, mean, math.nan, 1.0, math.nan, math.nan, n < SMALL_SAMPLE)
    se = float(x.std(ddof=1)) / math.sqrt(n)
    t = mean / se
    p = 2.0 * (1.0 - NormalDist().cdf(abs(t)))
    return MeanTest(n, mean, t, p, mean - 1.96 * se, mean + 1.96 * se, n < SMALL_SAMPLE)


def required_t(ideas: int, alpha: float = 0.05) -> float:
    """The t-statistic a result needs to clear after ``ideas`` rules have been tried."""
    if ideas < 1:
        raise ValueError("ideas must be at least 1")
    return NormalDist().inv_cdf(1.0 - alpha / (2.0 * ideas))


def trades_needed(edge: float, spread: float, z: float = 1.96) -> int | None:
    """Trades needed before an average ``edge`` stands clear of zero, given the typical swing
    (``spread``, the standard deviation of one trade). None when there is no edge to find."""
    if edge <= 0 or spread <= 0:
        return None
    return math.ceil((z * spread / edge) ** 2)


def sentence(r: MeanTest, unit: str, ideas: int) -> str:
    """One plain-English line for a report or Slack, e.g. unit='a month' or 'per trade'."""
    if r.n == 0:
        return "No results yet."
    if math.isnan(r.t):
        return f"{r.n} results with no spread to test, so nothing can be concluded."
    head = (
        f"Average {r.mean:+.2%} {unit} over {r.n} results "
        f"(95% range {r.low:+.2%} to {r.high:+.2%})."
    )
    if r.small_sample:
        return f"{head} Fewer than {SMALL_SAMPLE} results, so this is only a rough guide."
    if r.low > 0 and r.t >= required_t(ideas):
        return f"{head} That clears the stricter bar for the {ideas} ideas tried so far."
    if r.low > 0:
        return (
            f"{head} It beats zero at the usual 5% level but not the stricter bar for the "
            f"{ideas} ideas tried so far, so it could easily be luck."
        )
    return f"{head} The range includes zero, so this could easily be luck."


def monte_carlo(returns: Sequence[float], sims: int = 5000, seed: int = 0) -> dict[str, float]:
    """Resample the trades with replacement ``sims`` times (same count each time) and report the
    spread of outcomes. ``returns`` are each trade's gain as a fraction of starting equity, added
    up (no compounding). Shows how good or bad luck alone could have made the same trades look;
    it cannot create an edge. ``prob_loss`` is the share of runs that ended below zero and
    ``max_dd`` the worst peak-to-trough fall in a run, as a fraction of starting equity."""
    x = np.asarray([v for v in returns if not math.isnan(v)], dtype=float)
    if len(x) == 0:
        return {}
    rng = np.random.default_rng(seed)
    paths = np.cumsum(rng.choice(x, size=(sims, len(x)), replace=True), axis=1)
    total = paths[:, -1]
    peak = np.maximum.accumulate(np.concatenate([np.zeros((sims, 1)), paths], axis=1), axis=1)
    dd = (peak[:, 1:] - paths).max(axis=1)
    return {
        "prob_loss": float((total < 0).mean()),
        "p5_total": float(np.percentile(total, 5)),
        "median_total": float(np.median(total)),
        "p95_total": float(np.percentile(total, 95)),
        "median_max_dd": float(np.median(dd)),
        "p95_max_dd": float(np.percentile(dd, 95)),
    }
