# Author: Kai0421
import importlib.util
from datetime import datetime
from pathlib import Path

from bot.data import ET

spec = importlib.util.spec_from_file_location(
    "council_paper", Path(__file__).parents[1] / "scripts" / "research" / "council_paper.py"
)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


def test_slack_events_wait_for_0830_et():
    # 12:30 UK in the week UK clocks are back but US clocks are not = 07:30 ET: too early
    assert not cp.send_due(datetime(2026, 10, 26, 7, 30, tzinfo=ET))
    assert cp.send_due(datetime(2026, 10, 26, 8, 30, tzinfo=ET))  # 13:30 UK that week
    assert cp.send_due(datetime(2026, 10, 19, 8, 30, tzinfo=ET))  # 13:30 UK in summer


def test_upgrade_is_told_once_and_retried_if_slack_fails():
    from bot import ricks_paper as rp

    sent = []
    state = {"rules_version": "v1"}
    cp.tell_upgrade(state, lambda title, text: sent.append(title))
    assert sent == [f"Council of Ricks upgraded to {rp.RULES_VERSION}"]
    assert state["rules_version"] == rp.RULES_VERSION
    cp.tell_upgrade(state, lambda title, text: sent.append(title))  # same version: silent
    assert len(sent) == 1

    def broken(title, text):
        raise RuntimeError("slack down")

    retry = {"rules_version": "v1"}
    cp.tell_upgrade(retry, broken)
    assert retry["rules_version"] == "v1"  # not marked, so the next run tries again

    first = {}
    cp.tell_upgrade(first, broken)  # first ever run: record the version, say nothing
    assert first["rules_version"] == rp.RULES_VERSION
