# Author: Kai0421
from datetime import UTC, date, datetime, timedelta

from bot import council_health as ch
from bot.health import TaskInfo

NOW = datetime(2026, 10, 20, 12, 45, tzinfo=UTC)  # Tue 08:45 ET


def task(name, hours_ago=0.2, state="Ready", result=0):
    last = None if hours_ago is None else NOW - timedelta(hours=hours_ago)
    return TaskInfo(name, state, last, result)


def good(**over) -> ch.CouncilInputs:
    base = {
        "now": NOW,
        "tasks": [
            task("council-shadow"),
            task("sentiment-collect"),
            task("council-paper", hours_ago=20),
        ],
        "shadow_last_day": date(2026, 10, 19),
        "shadow_days": 6,
        "collector_last_full_day": date(2026, 10, 19),
        "ledger_last_day": date(2026, 10, 19),
        "summary_day": date(2026, 10, 19),
        "rules_version": "v2",
        "open_trades": 3,
        "closed_trades": 1,
    }
    return ch.CouncilInputs(**{**base, **over})


def test_all_fine_says_so_and_is_info():
    level, title, text = ch.render(good())
    assert (level, title) == ("INFO", "Council health: OK")
    assert "Everything is running" in text
    assert "1 of 100" in text and "v2" in text


def test_a_failed_or_disabled_task_is_named_in_plain_english():
    bad = good(
        tasks=[task("council-shadow", result=1), task("sentiment-collect", state="Disabled")]
    )
    level, title, text = ch.render(bad)
    assert level == "WARN" and title == "Council health: ATTENTION"
    assert "council-shadow" in text and "failed" in text
    assert "sentiment-collect" in text and "switched off" in text


def test_a_missing_task_and_a_stale_task_are_flagged():
    issues = ch.assess(good(tasks=[task("council-shadow", hours_ago=9)]))
    joined = " ".join(issues)
    assert "has not run for 9 hours" in joined
    assert "sentiment-collect" in joined and "not found" in joined


def test_running_or_never_run_codes_are_not_failures():
    t = [task("council-shadow", result=267009), task("sentiment-collect", result=267011)]
    tasks = [*t, task("council-paper", hours_ago=20)]
    assert ch.assess(good(tasks=tasks)) == []


def test_stale_data_and_missing_shadow_day_are_flagged():
    stale = good(collector_last_full_day=date(2026, 10, 16), shadow_last_day=date(2026, 10, 16))
    issues = " ".join(ch.assess(stale))
    assert "last fully collected for 16 Oct" in issues
    assert "last recorded council day is 16 Oct" in issues


def test_monday_morning_expects_friday_not_sunday():
    mon = datetime(2026, 10, 19, 12, 45, tzinfo=UTC)
    fresh = [TaskInfo(n, "Ready", mon - timedelta(hours=1), 0) for n in ch.TASK_MAX_AGE_H]
    inputs = good(
        now=mon,
        tasks=fresh,
        shadow_last_day=date(2026, 10, 16),
        collector_last_full_day=date(2026, 10, 16),
        ledger_last_day=date(2026, 10, 16),
        summary_day=date(2026, 10, 16),
    )
    assert ch.expected_session(mon) == date(2026, 10, 16)
    assert ch.assess(inputs) == []


def test_unsent_daily_summary_is_flagged():
    issues = " ".join(ch.assess(good(summary_day=date(2026, 10, 16))))
    assert "daily summary" in issues and "not been sent" in issues


def test_no_data_yet_does_not_crash():
    empty = good(
        shadow_last_day=None,
        collector_last_full_day=None,
        ledger_last_day=None,
        summary_day=None,
    )
    level, _, text = ch.render(empty)
    assert level == "WARN" and "no council days recorded yet" in text


def test_text_is_plain_english_and_never_critical():
    for inputs in (good(), good(tasks=[], collector_last_full_day=None)):
        level, _, text = ch.render(inputs)
        assert level in ("INFO", "WARN")
        for bad in ("None", "nan", "Traceback", "267009"):
            assert bad not in text
