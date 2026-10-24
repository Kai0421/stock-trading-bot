# Author: Kai0421
"""Tests for bot.report: weekly metrics, charts, HTML, summary.json, pack and Slack post."""

import json
from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot.journal import Journal
from bot.notify import Delivery
from bot.report import period_metrics, render_charts, run_weekly_report, week_bounds

ET = ZoneInfo("America/New_York")


def trade(exit_day, pnl, r, sig="up|mid|strong|deep|near", hold=3, reason="trail"):
    return {
        "symbol": "AAPL",
        "entry_date": "2026-09-01",
        "exit_date": exit_day,
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl / 10,
        "shares": 10,
        "pnl": pnl,
        "r_multiple": r,
        "exit_reason": reason,
        "holding_days": hold,
        "signature": sig,
        "win_prob": 0.72,
    }


def equity_frame(values, end="2026-10-09"):
    idx = pd.bdate_range(end=end, periods=len(values))
    df = pd.DataFrame({"equity": values, "cash": [v * 0.5 for v in values]}, index=idx)
    df["peak"] = df["equity"].cummax()
    df["drawdown"] = 1 - df["equity"] / df["peak"]
    return df


def test_week_bounds_for_saturday():
    assert week_bounds(date(2026, 10, 10)) == ("2026-W41", date(2026, 10, 5), date(2026, 10, 9))


def test_period_metrics_known_values():
    trades = [trade("2026-10-06", 30, 1.5), trade("2026-10-07", 20, 1.0),
              trade("2026-10-08", -10, -0.5), trade("2026-10-09", 10, 0.5)]  # fmt: skip
    eq = equity_frame([100_000, 101_000, 99_000, 102_000, 103_000])
    spy = pd.Series([400.0, 402.0, 398.0, 404.0, 408.0], index=eq.index)
    m = period_metrics(trades, eq, spy)
    assert m["trades"] == 4
    assert m["win_rate"] == pytest.approx(0.75)
    assert m["expectancy_r"] == pytest.approx(0.625)
    assert m["avg_win_r"] == pytest.approx(1.0)
    assert m["avg_loss_r"] == pytest.approx(-0.5)
    assert m["profit_factor"] == pytest.approx(60 / 10)
    assert m["return"] == pytest.approx(0.03)
    assert m["spy_return"] == pytest.approx(0.02)
    assert m["max_drawdown"] == pytest.approx(1 - 99_000 / 101_000)
    assert m["avg_holding_days"] == pytest.approx(3.0)
    assert 0 < m["wilson_lb"] < 0.75


def test_period_metrics_empty():
    m = period_metrics([], equity_frame([100_000]), pd.Series(dtype=float))
    assert m["trades"] == 0 and m["win_rate"] is None and m["profit_factor"] is None


def test_render_charts_writes_six_pngs(tmp_path):
    eq = equity_frame(list(np.linspace(100_000, 104_000, 30)))
    spy = pd.Series(np.linspace(400, 410, 30), index=eq.index)
    trades = [
        trade("2026-10-06", 30, 1.5),
        trade("2026-10-07", -10, -0.5, sig="up|low|weak|deep|far"),
    ]
    files = render_charts(tmp_path, eq, spy, trades, week_start=date(2026, 10, 5))
    assert len(files) == 6
    assert all(f.exists() and f.read_bytes()[:4] == b"\x89PNG" for f in files)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, level, title, text="", files=(), thread=None, category="bot"):
        self.sent.append((title, text, list(files)))
        return Delivery(True, "171.5")


def spy_closes():
    """Known SPY closes: 100 until 2026-08-31, then fixed anchors (others 150).

    Week window starts on the previous Friday 2026-10-02 (250), the 4-week window on
    Friday 2026-09-11 (200); the series ends on Friday 2026-10-09 (275).
    """
    idx = pd.bdate_range("2016-01-04", "2026-10-09")
    close = pd.Series(100.0, index=idx)
    close[close.index >= "2026-09-01"] = 150.0
    for day, v in {"2026-09-01": 160.0, "2026-09-11": 200.0, "2026-10-02": 250.0,
                   "2026-10-09": 275.0}.items():  # fmt: skip
        close[pd.Timestamp(day)] = v
    return pd.DataFrame({"close": close})


def weekly_engine(tmp_path, journal):
    spy = spy_closes()
    return NS(
        journal=journal,
        notifier=FakeNotifier(),
        memory_dir=tmp_path / "memory",
        clock=lambda: datetime(2026, 10, 10, 9, 0, tzinfo=ET),
        load_bars=lambda symbols, start, end: {
            "SPY": spy.loc[pd.Timestamp(start) : pd.Timestamp(end)]
        },
    )


def test_weekly_spy_return_per_window_without_equity(tmp_path):
    # No equity history yet (no trades): SPY must still be measured per window, not since
    # the first bar ever loaded; with no inception there is no "all" benchmark.
    out = run_weekly_report(weekly_engine(tmp_path, Journal(tmp_path / "bot.db")),
                            reports_dir=tmp_path / "reports")  # fmt: skip
    periods = json.loads((out / "summary.json").read_text())["periods"]
    assert periods["week"]["spy_return"] == pytest.approx(275 / 250 - 1)
    assert periods["4 weeks"]["spy_return"] == pytest.approx(275 / 200 - 1)
    assert periods["all"]["spy_return"] is None


def test_weekly_spy_return_matches_equity_windows(tmp_path):
    j = Journal(tmp_path / "bot.db")
    days = pd.bdate_range("2026-09-01", "2026-10-09")
    for i, d in enumerate(days):
        j.record_equity(d.date(), 100_000 + 100 * i, 50_000)
    out = run_weekly_report(weekly_engine(tmp_path, j), reports_dir=tmp_path / "reports")
    periods = json.loads((out / "summary.json").read_text())["periods"]
    last = 100_000 + 100 * (len(days) - 1)
    pos = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(days)}
    assert periods["week"]["return"] == pytest.approx(
        last / (100_000 + 100 * pos["2026-10-02"]) - 1
    )
    assert periods["week"]["spy_return"] == pytest.approx(275 / 250 - 1)
    assert periods["4 weeks"]["return"] == pytest.approx(
        last / (100_000 + 100 * pos["2026-09-11"]) - 1
    )
    assert periods["4 weeks"]["spy_return"] == pytest.approx(275 / 200 - 1)
    # Since inception (2026-09-01), not from the bars loaded a week before it.
    assert periods["all"]["return"] == pytest.approx(last / 100_000 - 1)
    assert periods["all"]["spy_return"] == pytest.approx(275 / 160 - 1)


def test_run_weekly_report_end_to_end(tmp_path):
    j = Journal(tmp_path / "bot.db")
    for i, v in enumerate([100_000, 100_500, 101_000, 100_800, 101_200]):
        j.record_equity(date(2026, 10, 5 + i), v, v / 2)
    j.record_trade("AAPL", date(2026, 9, 30), date(2026, 10, 7), 100.0, 104.0, 10, 40.0, 1.0,
                   "trail", "up|mid|strong|deep|near", 0.72)  # fmt: skip
    j.record_signal(date(2026, 10, 6), "MSFT", "sig", None, None, 0, False, "probability gate", {})
    spy_idx = pd.bdate_range("2026-01-01", "2026-10-09")
    spy = pd.DataFrame({"close": np.linspace(380, 410, len(spy_idx))}, index=spy_idx)
    notifier = FakeNotifier()
    engine = NS(
        journal=j,
        notifier=notifier,
        memory_dir=tmp_path / "memory",
        clock=lambda: datetime(2026, 10, 10, 9, 0, tzinfo=ET),
        load_bars=lambda symbols, start, end: {"SPY": spy},
    )
    out = run_weekly_report(engine, reports_dir=tmp_path / "reports")
    assert out == tmp_path / "reports" / "2026-W41"
    summary = json.loads((out / "summary.json").read_text())
    assert summary["week"] == "2026-W41"
    assert summary["periods"]["week"]["trades"] == 1
    assert summary["rule_audit"]["reached_broker"] == 0
    html = (out / "report.html").read_text()
    assert html.count("data:image/png;base64,") == 6
    pack = tmp_path / "memory" / "packs" / "weekly-2026-W41.md"
    assert pack.exists() and len(pack.read_text()) <= 16_000
    title, text, files = notifier.sent[0]
    assert "2026-W41" in title and len(files) == 6
    assert j.get_kv("slack_thread:weekly") == "171.5"
