# Author: Kai0421
"""Tests for bot.usage: Claude usage snapshot, low-usage alerts and the AI-job gate."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from bot.notify import Level
from bot.usage import Snapshot, UsageWindow, evaluate, fire_routine, read_snapshot

NOW = datetime(2026, 10, 5, 21, 52, tzinfo=UTC)


def snap(five=None, week=None, age=timedelta(minutes=10), five_reset_h=3, week_reset_d=3):
    return Snapshot(
        captured_at=NOW - age,
        five_hour=None if five is None else UsageWindow(five, NOW + timedelta(hours=five_reset_h)),
        seven_day=None if week is None else UsageWindow(week, NOW + timedelta(days=week_reset_d)),
    )


def test_read_snapshot_from_statusline_file(tmp_path):
    path = tmp_path / "claude_usage.json"
    reset = int((NOW + timedelta(hours=2)).timestamp())
    path.write_text(
        json.dumps(
            {
                "captured_at": "2026-10-05T21:40:00Z",
                "five_hour": {"used_percentage": 42.5, "resets_at": reset},
                "seven_day": {"used_percentage": 61, "resets_at": reset + 86400},
            }
        )
    )
    s = read_snapshot(path)
    assert s.captured_at == datetime(2026, 10, 5, 21, 40, tzinfo=UTC)
    assert s.five_hour.used_percentage == 42.5
    assert s.seven_day.resets_at == datetime.fromtimestamp(reset + 86400, UTC)


@pytest.mark.parametrize("content", [None, "not json", "{}"])
def test_read_snapshot_missing_or_corrupt(tmp_path, content):
    path = tmp_path / "u.json"
    if content is not None:
        path.write_text(content)
    s = read_snapshot(path)
    assert s is None or (s.five_hour is None and s.seven_day is None)


def levels(result):
    return [a.level for a in result.alerts]


def test_quiet_when_low():
    r = evaluate(snap(five=10, week=20), NOW)
    assert r.alerts == [] and r.gate_ok


def test_weekly_thresholds():
    assert levels(evaluate(snap(week=55), NOW)) == [Level.INFO]
    assert levels(evaluate(snap(week=80), NOW)) == [Level.WARN]
    r = evaluate(snap(week=92), NOW)
    assert levels(r) == [Level.WARN]
    assert not r.gate_ok and "7-day" in r.gate_reason


def test_alert_keys_are_per_reset_window():
    a = evaluate(snap(week=80, week_reset_d=3), NOW).alerts[0].key
    b = evaluate(snap(week=80, week_reset_d=4), NOW).alerts[0].key
    assert a != b and a.startswith("usage:7d:75:")


def test_five_hour_warning_only_if_job_due_before_reset():
    due = NOW + timedelta(minutes=15)
    assert levels(evaluate(snap(five=82), NOW, ai_job_due=due)) == [Level.WARN]
    assert evaluate(snap(five=82), NOW, ai_job_due=NOW + timedelta(hours=5)).alerts == []
    assert evaluate(snap(five=82), NOW).alerts == []


def test_five_hour_gate():
    r = evaluate(snap(five=86), NOW)
    assert not r.gate_ok and "5-hour" in r.gate_reason
    assert evaluate(snap(five=84), NOW).gate_ok


def test_expired_windows_are_ignored():
    r = evaluate(snap(five=99, week=99, five_reset_h=-1, week_reset_d=-1), NOW)
    assert r.gate_ok
    assert r.alerts == []


def test_missing_snapshot_is_silent_and_does_not_block():
    """No usage data at all (e.g. on GitHub Actions): stay quiet, let reviews run."""
    r = evaluate(None, NOW)
    assert r.gate_ok
    assert r.alerts == []


def test_stale_snapshot_is_info_and_does_not_block():
    r = evaluate(snap(week=10, age=timedelta(hours=49)), NOW)
    assert r.gate_ok
    assert [a.level for a in r.alerts] == [Level.INFO]
    assert "unknown" in r.alerts[0].text


def test_fire_routine_request():
    calls = []

    class Resp:
        status_code = 200

    def post(url, **kw):
        calls.append((url, kw))
        return Resp()

    assert fire_routine("https://api/fire", "tok", "pack text", post=post)
    url, kw = calls[0]
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["headers"]["anthropic-beta"] == "experimental-cc-routine-2026-04-01"
    assert kw["headers"]["anthropic-version"] == "2023-06-01"  # API rejects requests without it
    assert kw["json"] == {"text": "pack text"}


def test_fire_routine_failures_return_false():
    class Bad:
        status_code = 429

    assert not fire_routine("u", "t", "x", post=lambda url, **kw: Bad())
    assert not fire_routine("", "t", "x", post=lambda url, **kw: Bad())

    def boom(url, **kw):
        raise OSError("network")

    assert not fire_routine("u", "t", "x", post=boom)
