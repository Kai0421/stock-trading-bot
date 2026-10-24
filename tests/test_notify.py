# Author: Kai0421
"""Tests for bot.notify: Slack and macOS notifiers and the never-raising dispatcher."""

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Settings
from bot.journal import Journal
from bot.notify import (
    Delivery,
    Dispatcher,
    Level,
    MacNotifier,
    NotifyError,
    SlackBotNotifier,
    SlackWebhookNotifier,
    build_dispatcher,
    main,
)


class Resp:
    def __init__(self, status=200):
        self.status_code = status
        self.text = "ok"


class Recorder:
    def __init__(self, status=200, raises=None):
        self.calls = []
        self.status = status
        self.raises = raises

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.raises:
            raise self.raises
        return Resp(self.status)


class FakeNotifier:
    def __init__(self, name, fail=0):
        self.name = name
        self.fail = fail
        self.sent = []

    def send(self, level, title, text, files=(), thread=None):
        if self.fail:
            self.fail -= 1
            raise NotifyError(f"{self.name} down")
        self.sent.append((level, title, text, tuple(files), thread))
        return f"{self.name}-ref"


# ------------------------------------------------------- notifiers


def test_webhook_payload_and_channel_ping():
    post = Recorder()
    n = SlackWebhookNotifier("https://hooks.slack.com/x", post=post)
    n.send(Level.INFO, "EOD", "equity up")
    n.send(Level.CRITICAL, "Kill switch", "drawdown")
    (url, kw1), (_, kw2) = post.calls
    assert url == "https://hooks.slack.com/x"
    assert "*EOD*" in kw1["json"]["text"] and "<!channel>" not in kw1["json"]["text"]
    assert kw2["json"]["text"].startswith("<!channel>")
    assert kw1["timeout"] == 5


def test_slack_text_is_plain_with_icons_only_for_problems():
    from bot.notify import slack_text

    assert slack_text(Level.INFO, "Bought AAPL", "10 shares") == "*Bought AAPL*\n10 shares"
    assert slack_text(Level.WARN, "No price", "").startswith(":warning: *No price*")
    assert slack_text(Level.CRITICAL, "x", "y").startswith("<!channel> :rotating_light: *x*")
    assert "[INFO]" not in slack_text(Level.INFO, "t", "x")


def test_money_share_and_reason_wording():
    from bot.notify import reason_text, shares, usd

    assert usd(1234.5) == "$1,234.50" and usd(-5) == "-$5.00"
    assert shares(1) == "1 share" and shares(10) == "10 shares" and shares(2.5) == "2.5 shares"
    assert reason_text("stop") == "the stop-loss was hit"
    assert reason_text("time_stop") == "time stop"


def test_webhook_error_status_raises():
    with pytest.raises(NotifyError):
        SlackWebhookNotifier("u", post=Recorder(status=500)).send(Level.INFO, "t", "x")


def test_mac_notifier_escapes_quotes():
    calls = []
    MacNotifier(run=lambda args, **kw: calls.append(args)).send(Level.WARN, 'a "b"', 'c "d"')
    script = calls[0][-1]
    assert calls[0][0] == "osascript"
    assert '\\"b\\"' in script and '\\"d\\"' in script


class FakeSlackClient:
    def __init__(self):
        self.posts = []
        self.uploads = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": "111.222", "channel": "C123"}

    def files_upload_v2(self, **kw):
        self.uploads.append(kw)
        return {"ok": True}


def test_slack_bot_posts_then_uploads_files_in_thread(tmp_path):
    img = tmp_path / "equity.png"
    img.write_bytes(b"png")
    client = FakeSlackClient()
    ref = SlackBotNotifier("xoxb", "#trading-bot", client=client).send(
        Level.INFO, "Weekly", "stats", files=[img], thread="999.000"
    )
    assert ref == "111.222"
    assert client.posts[0]["channel"] == "#trading-bot"
    assert client.posts[0]["thread_ts"] == "999.000"
    upload = client.uploads[0]
    assert upload["channel"] == "C123"  # files_upload_v2 needs the channel ID
    assert upload["thread_ts"] == "111.222"
    assert upload["file_uploads"][0]["file"] == str(img)


# ------------------------------------------------------- dispatcher


def test_dispatcher_goes_to_slack_only_when_it_works():
    slack, mac = FakeNotifier("slack"), FakeNotifier("mac")
    d = Dispatcher(slack=[slack], fallback=[mac])
    assert d.notify(Level.CRITICAL, "t", "x") == Delivery(True, "slack-ref")
    assert len(slack.sent) == 1 and not mac.sent


def test_dispatcher_retries_once_then_falls_back():
    slack = FakeNotifier("slack", fail=2)  # fails both attempts
    mac = FakeNotifier("mac")
    d = Dispatcher(slack=[slack], fallback=[mac])
    assert d.notify(Level.WARN, "t", "x").ok
    assert mac.sent


def test_dispatcher_retry_succeeds():
    slack = FakeNotifier("slack", fail=1)
    assert Dispatcher(slack=[slack], fallback=[]).notify(Level.INFO, "t", "x").ok
    assert len(slack.sent) == 1


def test_dispatcher_never_raises():
    class Boom:
        name = "boom"

        def send(self, *a, **k):
            raise RuntimeError("unexpected")

    d = Dispatcher(slack=[Boom()], fallback=[Boom()])
    assert d.notify(Level.CRITICAL, "t", "x") == Delivery(False, None)


def test_dispatcher_dedupes(tmp_path):
    j = Journal(tmp_path / "bot.db")
    now = datetime(2026, 10, 5, 16, tzinfo=UTC)
    clock = iter([now, now + timedelta(minutes=5), now + timedelta(minutes=40)])
    slack = FakeNotifier("slack")
    d = Dispatcher(
        slack=[slack],
        fallback=[],
        dedupe=lambda key: j.should_send(key, next(clock), timedelta(minutes=30)),
    )
    for _ in range(3):
        d.notify(Level.INFO, "same", "alert")
    assert len(slack.sent) == 2


def test_build_dispatcher_from_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")  # the macOS fallback only exists on a Mac
    s = Settings(slack_webhook_url="https://hooks", db_path=tmp_path / "bot.db")
    d = build_dispatcher(s, Journal(s.db_path))
    assert [n.name for n in d.slack] == ["slack-webhook"]
    assert [n.name for n in d.fallback] == ["macos"]
    bare = build_dispatcher(Settings(db_path=tmp_path / "x.db"), None)
    assert bare.slack == [] and [n.name for n in bare.fallback] == ["macos"]


def test_council_messages_use_the_council_channel_when_it_is_set(tmp_path):
    s = Settings(
        slack_bot_token="xoxb-1",
        slack_channel="#trading-bot",
        council_slack_channel="#council-of-ricks",
        db_path=tmp_path / "bot.db",
    )
    assert build_dispatcher(s, None, council=True).slack[0]._channel == "#council-of-ricks"
    assert build_dispatcher(s, None).slack[0]._channel == "#trading-bot"  # the bot is untouched
    unset = Settings(slack_bot_token="xoxb-1", slack_channel="#bots", db_path=tmp_path / "b.db")
    assert build_dispatcher(unset, None, council=True).slack[0]._channel == "#bots"


# ------------------------------------------------------- CLI


def test_cli_posts_file_contents(tmp_path, monkeypatch):
    note = tmp_path / "review.md"
    note.write_text("# review\nall good")
    sent = []

    class D:
        def notify(self, level, title, text="", files=(), thread=None, category="bot"):
            sent.append((level, title, text, thread))
            return Delivery(True, None)

    monkeypatch.setattr("bot.notify._dispatcher_from_env", lambda council=False: (D(), None))
    assert main(["--level", "INFO", "--file", str(note)]) == 0
    assert sent[0][0] is Level.INFO
    assert "all good" in sent[0][2]


def test_cli_thread_lookup(tmp_path, monkeypatch):
    j = Journal(tmp_path / "bot.db")
    j.set_kv("slack_thread:weekly", "555.1")
    sent = []

    class D:
        def notify(self, level, title, text="", files=(), thread=None, category="bot"):
            sent.append(thread)
            return Delivery(False, None)

    monkeypatch.setattr("bot.notify._dispatcher_from_env", lambda council=False: (D(), j))
    assert main(["--level", "INFO", "--thread", "weekly", "hello"]) == 1
    assert sent == ["555.1"]


def test_slack_bot_client_loads_ca_certificates():
    n = SlackBotNotifier("xoxb-test", "#c")  # real WebClient, no network at construction
    ctx = n._client.ssl
    assert ctx is not None
    assert ctx.cert_store_stats()["x509_ca"] > 0  # certifi bundle, not an empty system store


def test_macos_fallback_only_on_macos(tmp_path, monkeypatch):
    s = Settings(slack_webhook_url="https://hooks", db_path=tmp_path / "bot.db")
    monkeypatch.setattr("sys.platform", "linux")
    assert build_dispatcher(s, None).fallback == []
    monkeypatch.setattr("sys.platform", "darwin")
    assert [n.name for n in build_dispatcher(s, None).fallback] == ["macos"]


def test_each_category_has_its_own_channel_and_falls_back_to_the_bot_channel(tmp_path):
    s = Settings(
        slack_bot_token="xoxb-1",
        slack_channel="#trading-bot",
        council_slack_channel="#council-of-ricks",
        general_slack_channel="#general",
        db_path=tmp_path / "bot.db",
    )
    chan = {c: build_dispatcher(s, None, category=c).slack[0]._channel for c in notify_categories()}
    assert chan == {"bot": "#trading-bot", "council": "#council-of-ricks", "general": "#general"}
    bare = Settings(slack_bot_token="xoxb-1", slack_channel="#bots", db_path=tmp_path / "b.db")
    for c in notify_categories():
        assert build_dispatcher(bare, None, category=c).slack[0]._channel == "#bots"


def notify_categories():
    from bot.notify import CATEGORIES

    return CATEGORIES


def test_one_dispatcher_routes_by_category_and_defaults_to_the_bot_channel():
    sent = []

    class Rec:
        def __init__(self, name):
            self.name = name

        def send(self, level, title, text, files=(), thread=None):
            sent.append((self.name, title))

    d = Dispatcher(
        slack=[Rec("bot")],
        fallback=[],
        routes={"general": [Rec("general")], "council": [Rec("council")]},
    )
    d.notify(Level.INFO, "a", "x")
    d.notify(Level.INFO, "b", "x", category="general")
    d.notify(Level.INFO, "c", "x", category="council")
    d.notify(Level.INFO, "d", "x", category="unknown")  # never lost: goes to the bot channel
    assert sent == [("bot", "a"), ("general", "b"), ("council", "c"), ("bot", "d")]


def test_cli_category_flag_and_the_council_alias(monkeypatch):
    seen = []

    class D:
        def notify(self, level, title, text="", files=(), thread=None, category="bot"):
            seen.append(category)
            return Delivery(True, None)

    made = []

    def fake_from_env(category="bot"):
        made.append(category)
        return D(), None

    monkeypatch.setattr("bot.notify._dispatcher_from_env", fake_from_env)
    assert main(["--category", "general", "hello"]) == 0
    assert main(["--council", "hello"]) == 0
    assert main(["hello"]) == 0
    assert made == ["general", "council", "bot"]  # the dispatcher is built for that channel


def test_a_dispatcher_built_for_a_category_sends_there_by_default(monkeypatch, tmp_path):
    """The CLI builds the dispatcher for its category and calls notify() without one."""
    sent = []

    class Rec:
        name = "rec"

        def __init__(self, token, channel):
            self.channel = channel

        def send(self, level, title, text, files=(), thread=None):
            sent.append(self.channel)

    monkeypatch.setattr("notifier.core.SlackBotNotifier", Rec)
    s = Settings(
        slack_bot_token="xoxb-1",
        slack_channel="#trading-bot",
        council_slack_channel="#council-of-ricks",
        general_slack_channel="#general",
        db_path=tmp_path / "bot.db",
    )
    for category, channel in (
        ("bot", "#trading-bot"),
        ("council", "#council-of-ricks"),
        ("general", "#general"),
    ):
        build_dispatcher(s, None, category=category).notify(Level.INFO, "t", "x")
        assert sent[-1] == channel, category
    # an explicit category still wins, and an unknown one falls back to the default route
    d = build_dispatcher(s, None, category="council")
    d.notify(Level.INFO, "t2", "x", category="general")
    assert sent[-1] == "#general"
    d.notify(Level.INFO, "t3", "x", category="nope")
    assert sent[-1] == "#council-of-ricks"
