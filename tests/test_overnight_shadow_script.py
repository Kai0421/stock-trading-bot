# Author: Kai0421
"""scripts/research/overnight_shadow.py: data requests stay inside the free plan."""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

from bot.data import ET, LoadResult

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research" / "overnight_shadow.py"


def load_script():
    spec = importlib.util.spec_from_file_location("overnight_shadow", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bars_end_before_the_sip_delay(monkeypatch, tmp_path):
    mod = load_script()
    monkeypatch.setattr(mod, "LOG", tmp_path / "shadow.csv")
    monkeypatch.setattr(mod, "make_data_client", lambda settings: object())
    seen = {}

    def fake_load(client, symbols, start, end, cache=None):
        seen["end"] = end
        return LoadResult(bars={})

    monkeypatch.setattr(mod, "load_daily_bars", fake_load)
    monkeypatch.setattr(mod, "record_shadow", lambda log, *a, **k: log)
    now = datetime(2026, 10, 9, 16, 25, tzinfo=ET)
    mod.run(now)
    # Free plan: no SIP data from the latest 15 minutes.
    assert seen["end"] <= now - timedelta(minutes=15)
