# Author: Kai0421
import importlib.util
from pathlib import Path

import pandas as pd

spec = importlib.util.spec_from_file_location(
    "exit_shadow", Path(__file__).parents[1] / "scripts" / "research" / "exit_shadow.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def bars(rows):
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {"high": [r[1] for r in rows], "low": [r[2] for r in rows], "close": [r[3] for r in rows]},
        index=idx,
    )


T = {
    "symbol": "X",
    "entry_date": "2026-10-01",
    "exit_date": "2026-10-03",
    "entry_price": 100.0,
    "initial_stop": 90.0,
    "exit_price": 103.0,
    "r_multiple": 0.3,
    "status": "closed",
}


def test_target_hit_before_the_live_exit_is_taken_at_the_target():
    out = mod.replay(
        T,
        bars(
            [
                ("2026-10-01", 105, 99, 101),
                ("2026-10-02", 116, 100, 104),
                ("2026-10-03", 104, 101, 103),
            ]
        ),
    )
    assert out["shadow_r"] == 1.5 and out["target_hit_on"] == "2026-10-02"


def test_stop_first_or_target_never_reached_matches_the_live_result():
    low = mod.replay(T, bars([("2026-10-01", 101, 89, 95), ("2026-10-02", 120, 95, 110)]))
    assert low["shadow_r"] == 0.3  # the stop came first
    flat = mod.replay(T, bars([("2026-10-01", 104, 99, 101), ("2026-10-02", 106, 100, 103)]))
    assert flat["shadow_r"] == 0.3 and flat["best_r_since_entry"] == 0.6


def test_verdict_says_no_when_the_shadow_is_not_clearly_better():
    done = pd.DataFrame(
        {"actual_r": [0.1, -0.5, 0.3, -0.2] * 8, "shadow_r": [0.1, -0.5, 0.3, -0.2] * 8}
    )
    title, text = mod.verdict(done)
    assert "32 finished" in title and "current exit stays" in text


def test_verdict_says_yes_only_when_clearly_better_in_both_halves():
    done = pd.DataFrame({"actual_r": [0.0, -0.1] * 16, "shadow_r": [0.5, 0.35] * 16})
    assert "looks good" in mod.verdict(done)[1]
