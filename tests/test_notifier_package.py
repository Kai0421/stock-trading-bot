# Author: Kai0421
import ast
from datetime import datetime, timedelta
from pathlib import Path

from notifier import Level, NotifierConfig, State, build_dispatcher
from notifier.__main__ import main

SRC = Path(__file__).resolve().parents[1] / "src" / "notifier"


def test_the_notifier_never_imports_the_trading_bot():
    """The whole point of the split: the notifier can change without touching trading code."""
    bad = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            bad += [f"{path.name}: {n}" for n in names if n == "bot" or n.startswith("bot.")]
    assert bad == []


def test_config_reads_channels_and_falls_back_to_the_bot_channel(tmp_path):
    cfg = NotifierConfig.from_env(
        {
            "SLACK_BOT_TOKEN": "xoxb-1",
            "SLACK_CHANNEL": "#trading-bot",
            "COUNCIL_SLACK_CHANNEL": "#council-of-ricks",
            "NOTIFIER_DB": str(tmp_path / "n.db"),
        }
    )
    assert cfg.channel("bot") == "#trading-bot"
    assert cfg.channel("council") == "#council-of-ricks"
    assert cfg.channel("general") == "#trading-bot"  # unset: falls back
    assert "xoxb-1" not in repr(cfg)  # secrets never show up in logs
    assert cfg.db_path == tmp_path / "n.db"


def test_state_dedupes_within_the_window_and_remembers_threads(tmp_path):
    st = State(tmp_path / "n.db")
    now = datetime(2026, 10, 19, 12, 0)
    assert st.should_send("k", now, timedelta(minutes=30)) is True
    assert st.should_send("k", now + timedelta(minutes=5), timedelta(minutes=30)) is False
    assert st.should_send("k", now + timedelta(minutes=31), timedelta(minutes=30)) is True
    assert st.get_kv("slack_thread:weekly") is None
    st.set_kv("slack_thread:weekly", "123.45")
    assert st.get_kv("slack_thread:weekly") == "123.45"
    st.close()


def test_a_dispatcher_from_config_routes_by_category_without_any_bot_code():
    sent = []

    class Rec:
        name = "rec"

        def __init__(self, token, channel):
            self.channel = channel

        def send(self, level, title, text, files=(), thread=None):
            sent.append(self.channel)

    import notifier.core as core

    original = core.SlackBotNotifier
    core.SlackBotNotifier = Rec
    try:
        cfg = NotifierConfig(
            slack_bot_token="x",
            slack_channel="#a",
            council_slack_channel="#b",
            general_slack_channel="#c",
        )
        for category, channel in (("bot", "#a"), ("council", "#b"), ("general", "#c")):
            build_dispatcher(cfg, category=category).notify(Level.INFO, "t", "x")
            assert sent[-1] == channel
    finally:
        core.SlackBotNotifier = original


def test_standalone_cli_reports_not_delivered_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    for var in ("SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOTIFIER_DB", str(tmp_path / "n.db"))
    assert main(["--category", "general", "hello"]) == 1
    assert "not delivered" in capsys.readouterr().err


def _recorders(monkeypatch, fail_bot_token=False):
    sent = []

    class Bot:
        name = "slack-bot"

        def __init__(self, token, channel):
            self.channel = channel

        def send(self, level, title, text, files=(), thread=None):
            if fail_bot_token and self.channel != "#trading-bot":
                raise RuntimeError("channel_not_found")
            sent.append(("bot-token", self.channel, title))

    class Hook:
        name = "slack-webhook"

        def __init__(self, url, post=None):
            pass

        def send(self, level, title, text, files=(), thread=None):
            sent.append(("webhook", "fixed", title))

    import notifier.core as core

    monkeypatch.setattr(core, "SlackBotNotifier", Bot)
    monkeypatch.setattr(core, "SlackWebhookNotifier", Hook)
    return sent


CFG = dict(
    slack_bot_token="x",
    slack_webhook_url="https://hook",
    slack_channel="#trading-bot",
    council_slack_channel="#council-of-ricks",
    general_slack_channel="#general",
)


def test_council_and_general_never_fall_back_to_the_trading_bot_webhook(monkeypatch):
    sent = _recorders(monkeypatch, fail_bot_token=True)
    d = build_dispatcher(NotifierConfig(**CFG), category="council")
    out = d.notify(Level.INFO, "Council picks", "x")
    assert out.ok is False
    # the picks were NOT posted to the trading-bot webhook; only a short notice reached it
    assert ("webhook", "fixed", "Council picks") not in sent
    notices = [s for s in sent if s[2] == "A message could not be delivered"]
    assert len(notices) == 1 and notices[0][1] == "#trading-bot"


def test_the_bot_channel_keeps_its_webhook_backup(monkeypatch):
    sent = _recorders(monkeypatch)
    cfg = NotifierConfig(**CFG)
    routes = build_dispatcher(cfg).routes
    assert [n.name for n in routes["bot"]] == ["slack-bot", "slack-webhook"]
    assert [n.name for n in routes["council"]] == ["slack-bot"]
    assert [n.name for n in routes["general"]] == ["slack-bot"]
    assert sent == []


def test_without_a_bot_token_the_webhook_is_the_only_path_for_every_category(monkeypatch):
    _recorders(monkeypatch)
    cfg = NotifierConfig(slack_webhook_url="https://hook")
    routes = build_dispatcher(cfg).routes
    assert all(
        [n.name for n in routes[c]] == ["slack-webhook"] for c in ("bot", "council", "general")
    )
