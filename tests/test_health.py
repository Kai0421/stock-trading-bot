# Author: Kai0421
"""Morning health report: what counts as a problem, and what the message says."""

from datetime import UTC, datetime, timedelta

from bot import health as h

NOW = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)


def task(name="tick", minutes_ago=3, result=0, state="Ready"):
    return h.TaskInfo(name, state, NOW - timedelta(minutes=minutes_ago), result)


def inputs(**kw):
    base = dict(
        now=NOW,
        tasks=[task("tick"), task("overnight-shadow", 20), task("sentiment-collect", 10)],
        broker_error=None,
        account_status="active",
        account_blocked=False,
        equity=100_000.0,
        cash=100_000.0,
        positions=[],
        open_orders=[],
        flags={},
        last_runs={"premarket": "ok"},
        recent_trades=[],
        market_open=False,
        next_open="09:30 ET",
        on_battery=False,
        uptime_hours=20.0,
    )
    base.update(kw)
    return h.HealthInputs(**base)


def levels(issues):
    return {i.level for i in issues}


def test_healthy_bot_has_no_issues_and_reports_ok():
    inp = inputs()
    assert h.assess(inp) == []
    level, title, text = h.render(inp, [])
    assert level == "INFO" and "OK" in title
    assert "no open positions" in text.lower()


def test_broker_error_is_critical():
    issues = h.assess(inputs(broker_error="401 unauthorized"))
    assert levels(issues) == {"CRITICAL"}
    assert "401" in issues[0].text


def test_blocked_account_is_critical():
    assert "CRITICAL" in levels(h.assess(inputs(account_blocked=True)))


def test_position_without_a_stop_is_critical():
    pos = h.PositionLine("AAA", 10, 100.0, 101.0, 10.0, 0.01, has_stop=False)
    issues = h.assess(inputs(positions=[pos]))
    assert "CRITICAL" in levels(issues) and "AAA" in issues[0].text


def test_position_with_a_stop_is_fine():
    pos = h.PositionLine("AAA", 10, 100.0, 101.0, 10.0, 0.01, has_stop=True)
    assert h.assess(inputs(positions=[pos])) == []


def test_stale_tick_warns_then_goes_critical():
    assert "WARN" in levels(h.assess(inputs(tasks=[task("tick", 20)])))
    assert "CRITICAL" in levels(h.assess(inputs(tasks=[task("tick", 90)])))


def test_failed_task_warns_but_never_run_does_not():
    assert "WARN" in levels(h.assess(inputs(tasks=[task("tick"), task("overnight-shadow", 5, 1)])))
    never = h.TaskInfo("overnight-shadow", "Ready", None, 267011)
    assert h.assess(inputs(tasks=[task("tick"), never])) == []


def test_flag_and_failed_job_and_battery_warn():
    issues = h.assess(
        inputs(flags={"kill": "drawdown"}, last_runs={"premarket": "error"}, on_battery=True)
    )
    texts = " ".join(i.text for i in issues)
    assert levels(issues) == {"WARN"}
    assert "kill" in texts and "premarket" in texts and "battery" in texts.lower()


def test_report_lists_positions_trades_and_uses_worst_level():
    pos = h.PositionLine("AAA", 10, 100.0, 95.0, -50.0, -0.05, has_stop=False)
    inp = inputs(
        positions=[pos],
        open_orders=["sell stop AAA 10 @ 90"],
        recent_trades=[("BBB", "2026-10-09", 120.5, "overnight")],
    )
    issues = h.assess(inp)
    level, title, text = h.render(inp, issues)
    assert level == "CRITICAL" and "ATTENTION" in title
    assert "AAA" in text and "-5.0%" in text
    assert "sell stop AAA" in text
    assert "BBB" in text and "+120.50" in text


def test_daily_health_report_task_is_never_flagged_as_stale():
    assert h.assess(inputs(tasks=[task("tick"), task("health-report", 8 * 60)])) == []
    assert "WARN" in levels(
        h.assess(inputs(tasks=[task("tick"), task("overnight-shadow", 8 * 60)]))
    )


def test_position_being_sold_at_the_open_is_not_flagged_for_a_missing_stop():
    sell = h.PositionLine("NVDA", 87, 226.75, 224.0, -240.0, -0.012, False, exiting=True)
    assert h.assess(inputs(positions=[sell])) == []
    assert "selling at the open" in h.render(inputs(positions=[sell]), [])[2]


def test_council_and_weekly_tasks_are_not_judged_or_listed_in_the_bot_report():
    others = [
        task("council-paper", 59 * 60),
        task("council-scorecard", 14 * 60),
        task("learning-loop", 15 * 60),
        task("journal-backup", 47 * 60),
        task("milestone-check", 14 * 60, 1),
    ]
    inp = inputs(tasks=[task("tick"), *others])
    assert h.assess(inp) == []
    text = h.render(inp, [])[2]
    assert "tick" in text
    for name in ("council", "learning-loop", "journal-backup", "milestone"):
        assert name not in text


def test_a_stalled_message_queue_is_flagged():
    stuck = inputs(outbox_waiting=3, outbox_oldest_min=25.0)
    texts = [i.text for i in h.assess(stuck)]
    assert any("3 Slack message(s) are waiting" in t and "notifier-drain" in t for t in texts)
    fine = inputs(outbox_waiting=2, outbox_oldest_min=1.5)
    assert not any("waiting to be sent" in i.text for i in h.assess(fine))
    assert any("could not be sent" in i.text for i in h.assess(inputs(outbox_failed=2)))
