# Author: Kai0421
"""The data / decision / execution split changes no plans, orders or alerts.

Each scenario in ``golden_engine`` replays real engine jobs (swing and intraday) and is
compared with the trace recorded from main before the split (docs/19-layers.md).
"""

import json

import pytest

from golden_engine import GOLDEN, SCENARIOS, run_scenario

EXPECTED = json.loads(GOLDEN.read_text())


def test_golden_covers_every_scenario():
    assert set(EXPECTED) == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_same_trace_as_before_the_split(name, tmp_path):
    got = run_scenario(name, tmp_path / name)
    want = EXPECTED[name]
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        assert g["broker"] == w["broker"]
        assert g["alerts"] == w["alerts"]
        assert g["journal"] == w["journal"]
        assert g["memory"] == w["memory"]
