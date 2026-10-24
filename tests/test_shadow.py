# Author: Kai0421
"""Shadow settlement: every journalled signal, taken or not, graded on real bars."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from bot.config import RiskConfig, StrategyConfig
from bot.journal import Journal
from bot.shadow import (
    by_reason,
    calibration,
    render_markdown,
    settle,
    settle_signal,
    shadow_rows,
)
from bot.strategies.base import ExitAction, Strategy

RISK = RiskConfig()


class Toy(Strategy):
    """Stop 4 below the entry (ATR 2); sells on the 3rd close if still held."""

    name = "toy"

    @classmethod
    def default_config(cls):
        return StrategyConfig()

    def features(self, bars):
        return bars.assign(atr=2.0)

    def entry_tables(self, features, benchmark):
        return {
            s: pd.DataFrame({"setup": False, "signature": None, "score": 0.0}, index=f.index)
            for s, f in features.items()
        }

    def initial_stop(self, entry, atr):
        return entry - 2 * atr

    def on_close(self, pos, bar):
        if pos.days_held >= 3:
            return [ExitAction("time_stop", pos.shares)], pos.stop
        return [], pos.stop


def bars(closes, start="2026-09-01", lows=None):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype="float64")
    low = pd.Series(lows if lows is not None else [x - 0.5 for x in closes], index=idx)
    return pd.DataFrame(
        {"open": c, "high": c + 0.5, "low": low, "close": c, "volume": 1e6}, index=idx
    )


def signal(day="2026-09-02", **kw):
    row = {
        "id": 1,
        "day": day,
        "symbol": "AAA",
        "signature": "toy|x",
        "win_prob": 0.7,
        "taken": 0,
        "reason": "no slots",
        "features": '{"strategy": "toy", "atr": 2.0}',
    }
    row.update(kw)
    return row


def test_winner_exits_on_the_strategys_time_stop():
    df = bars([100, 100, 101, 102, 103, 104, 105])
    out = settle_signal(signal(), Toy().features(df), Toy(), RISK)
    assert out is not None
    # entry at the open of the signal day (2026-09-02), sold at the open after 3 closes
    assert out["entry_date"] == "2026-09-02"
    assert out["exit_reason"] == "time_stop"
    assert out["exit_date"] == "2026-09-07"
    assert out["win"] == 1
    assert out["r_multiple"] > 0


def test_loser_is_stopped_out_at_the_stop():
    df = bars([100, 100, 99, 98, 94, 94], lows=[99.5, 99.5, 98.5, 96.0, 93.0, 93.0])
    out = settle_signal(signal(), Toy().features(df), Toy(), RISK)
    assert out is not None
    assert out["exit_reason"] == "stop"
    assert out["win"] == 0
    assert out["r_multiple"] == pytest.approx(-1.0, abs=0.05)


def test_open_trade_is_not_settled_yet():
    df = bars([100, 100, 101])
    assert settle_signal(signal(), Toy().features(df), Toy(), RISK) is None


def test_no_bar_on_the_signal_day_is_not_settled():
    df = bars([100, 101, 102], start="2026-08-20")
    assert settle_signal(signal(day="2026-09-30"), Toy().features(df), Toy(), RISK) is None


def test_missing_atr_is_skipped():
    df = bars([100, 100, 101, 102, 103, 104, 105])
    row = signal(features='{"strategy": "toy"}')
    assert settle_signal(row, Toy().features(df), Toy(), RISK) is None


def journal_with_signals(tmp_path):
    j = Journal(tmp_path / "bot.db")
    feats = {"strategy": "toy", "atr": 2.0}
    j.record_signal(date(2026, 9, 2), "AAA", "toy|x", 0.7, 0.6, 0, True, "planned", feats)
    j.record_signal(date(2026, 9, 2), "BBB", "toy|x", 0.66, 0.6, 0, False, "no slots", feats)
    j.record_signal(date(2026, 9, 2), "CCC", None, None, None, 0, False, "no setup", feats)
    j.record_signal(
        date(2026, 9, 2), "DDD", "toy|x", 0.7, 0.6, 0, False, "entered",
        {"strategy": "toy", "atr": 2.0, "mode": "intraday"},
    )  # fmt: skip
    return j


def test_settle_grades_daily_signals_once_and_skips_intraday_and_unsigned(tmp_path):
    j = journal_with_signals(tmp_path)
    up = bars([100, 100, 101, 102, 103, 104, 105])
    down = bars([100, 100, 99, 98, 94, 94], lows=[99.5, 99.5, 98.5, 96.0, 93.0, 93.0])
    calls = []

    def load(symbols, start, end):
        calls.append(sorted(symbols))
        return {"AAA": up, "BBB": down}

    n = settle(j, load, RISK, date(2026, 9, 10), strategy_for=lambda name: Toy())
    assert n == 2
    assert calls == [["AAA", "BBB"]]
    rows = {r["symbol"]: r for r in shadow_rows(j)}
    assert set(rows) == {"AAA", "BBB"}
    assert rows["AAA"]["win"] == 1 and rows["AAA"]["taken"] == 1
    assert rows["BBB"]["win"] == 0 and rows["BBB"]["reason"] == "no slots"
    # a second run adds nothing and fetches nothing
    assert settle(j, load, RISK, date(2026, 9, 10), strategy_for=lambda name: Toy()) == 0
    assert len(calls) == 1


def test_settle_skips_unknown_strategies(tmp_path):
    j = journal_with_signals(tmp_path)

    def factory(name):
        raise ValueError("unknown strategy")

    assert settle(j, lambda *a: {}, RISK, date(2026, 9, 10), strategy_for=factory) == 0


def rows_for(pairs):
    return [
        {"win_prob": p, "win": w, "r_multiple": 1.0 if w else -1.0, "taken": t, "reason": r}
        for p, w, t, r in pairs
    ]


def test_calibration_buckets_predicted_vs_realized():
    rows = rows_for(
        [(0.70, 1, 1, "planned"), (0.72, 0, 1, "planned"), (0.66, 1, 0, "no slots"),
         (0.50, 0, 0, "gate"), (None, 1, 0, "already held")]
    )  # fmt: skip
    table = {b["bucket"]: b for b in calibration(rows)}
    assert table["70%+"]["n"] == 2
    assert table["70%+"]["predicted"] == pytest.approx(0.71)
    assert table["70%+"]["realized"] == pytest.approx(0.5)
    assert table["65-70%"]["n"] == 1
    assert table["<55%"]["n"] == 1
    assert table["unknown"]["n"] == 1


def test_by_reason_splits_taken_from_each_rejection():
    rows = rows_for(
        [(0.7, 1, 1, "planned"), (0.7, 0, 1, "planned"), (0.66, 1, 0, "no slots"),
         (0.5, 0, 0, "gate"), (0.5, 0, 0, "gate")]
    )  # fmt: skip
    out = {r["reason"]: r for r in by_reason(rows)}
    assert out["taken"]["n"] == 2 and out["taken"]["win_rate"] == pytest.approx(0.5)
    assert out["gate"]["n"] == 2 and out["gate"]["mean_r"] == pytest.approx(-1.0)
    assert out["no slots"]["win_rate"] == pytest.approx(1.0)


def test_render_markdown_has_both_tables_and_handles_empty():
    assert "No shadow trades settled yet" in render_markdown([])
    text = render_markdown(rows_for([(0.7, 1, 1, "planned"), (0.5, 0, 0, "gate")]))
    assert "Calibration" in text and "By outcome of the signal" in text
    assert "| taken |" in text


def test_cli_reports_without_data_keys(tmp_path, capsys):
    from bot.shadow import main

    out = tmp_path / "shadow.md"
    assert main(["--out", str(out)], env={"BOT_DB": str(tmp_path / "bot.db")}) == 0
    assert "No shadow trades settled yet" in out.read_text()
    assert "Shadow trades" in capsys.readouterr().out


def test_pack_lines_are_compact_and_cover_both_views():
    from bot.shadow import pack_lines

    assert pack_lines([]) == []
    lines = pack_lines(rows_for([(0.7, 1, 1, "planned"), (0.66, 0, 0, "no slots")]))
    text = "\n".join(lines)
    assert "calib 70%+" in text and "taken" in text and "no slots" in text
    assert all(len(line) < 120 for line in lines)


def test_daily_pack_shows_shadow_section_when_given():
    from bot.pack import daily_pack

    kw = dict(
        day=date(2026, 10, 5), account={"equity": 1.0}, fills=[], exits=[], positions=[],
        prices={}, rejections={}, gate_health=[], flags={}, anomalies=[],
    )  # fmt: skip
    assert "Shadow trades" not in daily_pack(**kw)
    assert "- calib 70%+" in daily_pack(**kw, shadow=["- calib 70%+: n=1"])
