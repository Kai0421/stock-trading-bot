# Author: Kai0421
import math

import numpy as np
import pytest

from bot import luck_test as ev


def test_mean_test_matches_hand_calculation():
    x = [0.01, 0.03, -0.01, 0.02, 0.0, 0.02] * 10  # n = 60
    r = ev.mean_test(x)
    arr = np.array(x)
    se = arr.std(ddof=1) / math.sqrt(len(arr))
    assert r.n == 60 and abs(r.t - arr.mean() / se) < 1e-9
    assert abs(r.low - (arr.mean() - 1.96 * se)) < 0.001 and r.low < r.mean < r.high
    assert 0 < r.p < 0.05 and r.small_sample is False


def test_zero_mean_is_not_significant_and_an_interval_includes_zero():
    r = ev.mean_test([0.01, -0.01] * 50)
    assert abs(r.t) < 1e-9 and r.p == pytest.approx(1.0) and r.low < 0 < r.high


def test_tiny_or_flat_samples_do_not_crash():
    assert ev.mean_test([]).n == 0 and math.isnan(ev.mean_test([]).t)
    assert math.isnan(ev.mean_test([0.5]).t)
    flat = ev.mean_test([0.1] * 40)  # no spread: cannot test, never "infinitely significant"
    assert math.isnan(flat.t) and flat.p == 1.0
    assert ev.mean_test([0.01, 0.02, 0.03]).small_sample is True


def test_required_t_grows_with_the_number_of_ideas_tried():
    one = ev.required_t(1)
    assert one == pytest.approx(1.96, abs=0.01)
    assert ev.required_t(33) == pytest.approx(3.17, abs=0.02)
    assert ev.required_t(33) > ev.required_t(10) > one
    with pytest.raises(ValueError):
        ev.required_t(0)


def test_sentence_is_plain_english():
    weak = ev.sentence(ev.mean_test([0.01, -0.01] * 50), unit="a month", ideas=33)
    assert "could easily be luck" in weak
    strong = ev.sentence(ev.mean_test([0.02, 0.03, 0.025, 0.021] * 30), unit="a month", ideas=33)
    assert "clears" in strong
    for bad in ("nan", "None"):
        assert bad not in weak and bad not in strong


def test_trades_needed_for_an_edge():
    # an edge of 0.1R with a typical swing of 1R needs about (1.96 / 0.1)^2 = 384 trades
    assert ev.trades_needed(edge=0.1, spread=1.0) == pytest.approx(384, abs=2)
    assert ev.trades_needed(edge=0.0, spread=1.0) is None


def test_monte_carlo_all_wins_never_loses_and_is_repeatable():
    a = ev.monte_carlo([0.01, 0.02, 0.015] * 20, sims=500, seed=1)
    assert a["prob_loss"] == 0.0 and a["p5_total"] > 0 and a["median_max_dd"] >= 0
    assert a == ev.monte_carlo([0.01, 0.02, 0.015] * 20, sims=500, seed=1)


def test_monte_carlo_of_a_coin_flip_loses_about_half_the_time_and_has_drawdowns():
    r = ev.monte_carlo([0.01, -0.01] * 50, sims=2000, seed=2)
    assert 0.35 < r["prob_loss"] < 0.65
    assert r["p95_max_dd"] > r["median_max_dd"] > 0 and r["p5_total"] < 0 < r["p95_total"]


def test_monte_carlo_with_nothing_to_resample_is_empty_not_a_crash():
    assert ev.monte_carlo([], sims=10, seed=3) == {}
