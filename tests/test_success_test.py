# Author: Kai0421
import pandas as pd

from bot.success_test import judge, spy_same_exposure_return


def _rs(n, r):
    return [r + (0.2 if i % 2 else -0.2) for i in range(n)]


def test_too_few_trades_is_insufficient():
    v = judge(_rs(5, 0.5), [10.0] * 5, 0.05, 0.01)
    assert v.status == "INSUFFICIENT"


def test_clear_losses_fail_early():
    v = judge(_rs(40, -0.5), [-10.0] * 40, -0.05, 0.01)
    assert v.status == "FAIL_EARLY"


def test_pass_needs_edge_and_beating_spy():
    good = judge(_rs(100, 0.4), [10.0] * 100, 0.10, 0.04)
    assert good.status == "PASS"
    lagging = judge(_rs(100, 0.4), [10.0] * 100, 0.02, 0.04)
    assert lagging.status == "FAIL" and "SPY" in lagging.reasons[0]
    noisy = judge(_rs(100, 0.0), [1.0] * 100, 0.10, 0.04)
    assert noisy.status == "FAIL"


def test_spy_scaled_by_exposure():
    eq = pd.DataFrame(
        {"day": ["2026-10-13", "2026-10-14"], "equity": [100.0, 100.0], "cash": [50.0, 50.0]}
    )
    spy = pd.Series([100.0, 110.0], index=["2026-10-13", "2026-10-14"])
    assert abs(spy_same_exposure_return(eq, spy) - 0.05) < 1e-9
