# Author: Kai0421
import importlib.util
from datetime import datetime
from pathlib import Path

from bot.data import ET

spec = importlib.util.spec_from_file_location(
    "council_shadow", Path(__file__).parents[1] / "scripts" / "research" / "council_shadow.py"
)
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


def at(d, h, m=0):
    return datetime(2026, 10, d, h, m, tzinfo=ET)


def test_digest_waits_for_the_morning():
    assert not cs.digest_due(at(16, 22, 0), "2026-10-16")  # Friday night, data just arrived
    assert not cs.digest_due(at(17, 9, 0), "2026-10-16")  # Saturday: no trading day
    assert cs.digest_due(at(19, 8, 30), "2026-10-16")  # Monday 08:30 ET, before the open
    assert not cs.digest_due(at(19, 8, 29), "2026-10-16")
    assert not cs.digest_due(at(19, 9, 0), None)  # nothing pending
