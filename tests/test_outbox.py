# Author: Kai0421
from datetime import datetime, timedelta

from notifier import Delivery, Level
from notifier.outbox import Outbox, OutboxDispatcher, drain

T0 = datetime(2026, 10, 19, 12, 0)


class Direct:
    """Stands in for the real dispatcher: records what would be sent."""

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def notify(self, level, title, text="", files=(), thread=None, category=None):
        self.sent.append((level, title, text, category))
        return Delivery(self.ok, None)


def box(tmp_path):
    return Outbox(tmp_path / "outbox.db")


def test_queued_messages_come_back_in_order_and_survive_a_reopen(tmp_path):
    b = box(tmp_path)
    b.enqueue(Level.INFO, "a", "x", "bot", None, T0)
    b.enqueue(Level.WARN, "b", "y", "council", "weekly", T0)
    again = Outbox(tmp_path / "outbox.db")
    rows = again.pending()
    assert [r["title"] for r in rows] == ["a", "b"]
    assert rows[1]["category"] == "council" and rows[1]["thread"] == "weekly"


def test_drain_sends_each_message_to_its_category_and_marks_it_done(tmp_path):
    b, d = box(tmp_path), Direct()
    b.enqueue(Level.INFO, "plan", "x", "bot", None, T0)
    b.enqueue(Level.INFO, "picks", "y", "council", None, T0)
    assert drain(b, d, T0) == (2, 0)
    assert [(s[1], s[3]) for s in d.sent] == [("plan", "bot"), ("picks", "council")]
    assert b.pending() == [] and drain(b, d, T0) == (0, 0)  # nothing sent twice


def test_a_failed_send_is_retried_then_given_up_on(tmp_path):
    b, d = box(tmp_path), Direct(ok=False)
    b.enqueue(Level.INFO, "x", "y", "bot", None, T0)
    for _ in range(4):
        assert drain(b, d, T0, max_attempts=5)[1] == 1
        assert len(b.pending()) == 1  # still waiting
    drain(b, d, T0, max_attempts=5)
    assert b.pending() == [] and b.failed_count() == 1


def test_notify_queues_instead_of_sending_and_never_raises(tmp_path):
    b, d = box(tmp_path), Direct()
    od = OutboxDispatcher(b, d, clock=lambda: T0)
    out = od.notify(Level.INFO, "t", "x", category="general")
    assert out.ok and d.sent == [] and b.pending()[0]["category"] == "general"


def test_files_and_a_broken_outbox_fall_back_to_a_direct_send(tmp_path):
    d = Direct()
    od = OutboxDispatcher(box(tmp_path), d, clock=lambda: T0)
    od.notify(Level.INFO, "chart", "x", files=[tmp_path / "c.png"])
    assert len(d.sent) == 1  # attachments cannot wait in a queue

    class Broken:
        def enqueue(self, *a, **k):
            raise OSError("disk full")

    d2 = Direct()
    OutboxDispatcher(Broken(), d2, clock=lambda: T0).notify(Level.CRITICAL, "t", "x")
    assert len(d2.sent) == 1  # a message is never lost because the queue failed


def test_rescue_sends_only_overdue_critical_messages(tmp_path):
    b, d = box(tmp_path), Direct()
    now = {"t": T0}
    od = OutboxDispatcher(b, d, clock=lambda: now["t"], rescue_after=timedelta(minutes=3))
    od.notify(Level.CRITICAL, "no stop", "BLK")
    od.notify(Level.INFO, "plan", "x")
    now["t"] = T0 + timedelta(minutes=2)
    od.rescue()
    assert d.sent == []  # still inside the grace period
    now["t"] = T0 + timedelta(minutes=4)
    od.rescue()
    assert [s[1] for s in d.sent] == ["no stop"]  # the info message is left for the drain task
    od.rescue()
    assert len(d.sent) == 1  # and it is only rescued once
    assert [r["title"] for r in b.pending()] == ["plan"]


def test_the_engine_sends_directly_unless_the_outbox_switch_is_on(tmp_path):
    from bot.config import Settings
    from bot.notify import make_notifier
    from notifier import Dispatcher

    off = make_notifier(Settings(db_path=tmp_path / "b.db"), None)
    assert isinstance(off, Dispatcher)
    on = make_notifier(
        Settings(
            db_path=tmp_path / "b.db", notify_via_outbox=True, notifier_outbox=tmp_path / "o.db"
        ),
        None,
    )
    assert isinstance(on, OutboxDispatcher) and hasattr(on, "rescue")


def test_drain_command_sends_the_queue_with_the_notifiers_own_settings(tmp_path, monkeypatch):
    from notifier.__main__ import main, run_drain
    from notifier.config import NotifierConfig

    cfg = NotifierConfig(db_path=tmp_path / "n.db", outbox_path=tmp_path / "o.db")
    Outbox(cfg.outbox_path).enqueue(Level.INFO, "a", "x", "bot", None, T0)
    assert run_drain(cfg, T0) == (0, 1)  # no Slack credentials: stays queued, retried later
    assert len(Outbox(cfg.outbox_path).pending()) == 1
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTIFIER_DB", str(tmp_path / "n2.db"))
    monkeypatch.setenv("NOTIFIER_OUTBOX", str(tmp_path / "o2.db"))
    for var in ("SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    assert main(["drain"]) == 0
