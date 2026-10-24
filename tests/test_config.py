# Author: Kai0421
"""Tests for bot.config. The hard rules in AGENTS.md must be impossible to loosen."""

import dataclasses
from pathlib import Path

import pytest

from bot.config import (
    HARD_MIN_WIN_PROB_FLOOR,
    ConfigError,
    RiskConfig,
    Settings,
    StrategyConfig,
)


def test_defaults_match_docs():
    r = RiskConfig()
    assert r.risk_per_trade == 0.01
    assert r.max_open_positions == 6
    assert r.max_per_sector == 2
    assert r.max_position_pct == 0.20
    assert r.cash_reserve_pct == 0.02
    assert r.min_win_prob_target == 0.60  # owner, 2026-10-07
    assert r.min_win_prob_floor == 0.60
    assert r.min_signature_trades == 100
    assert r.daily_loss_kill_pct == 0.03
    assert r.drawdown_kill_pct == 0.10
    assert r.slippage_bps_round_trip == 10

    st = StrategyConfig()
    assert (st.sma_fast, st.sma_slow, st.ema_pullback) == (50, 200, 20)
    assert (st.rsi_period, st.rsi_entry_below, st.atr_period) == (2, 10, 14)
    assert (st.stop_atr_mult, st.trail_atr_mult) == (2.0, 3.0)
    assert st.time_stop_days == 10
    assert st.exit_set == "mean_reversion_full"  # paper experiment, decisions.md 2026-10-04
    assert st.earnings_blackout_days == 5


def test_hard_floor_constant_is_60_percent():
    assert HARD_MIN_WIN_PROB_FLOOR == 0.60


def test_config_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RiskConfig().min_win_prob_floor = 0.5


@pytest.mark.parametrize("floor", [0.59, 0.5, 0.0])
def test_refuses_win_prob_floor_below_60(floor):
    with pytest.raises(ConfigError, match="floor"):
        RiskConfig(min_win_prob_floor=floor)


def test_floor_can_be_raised():
    assert RiskConfig(min_win_prob_floor=0.68, min_win_prob_target=0.72).min_win_prob_floor == 0.68


def test_target_cannot_be_below_floor():
    with pytest.raises(ConfigError):
        RiskConfig(min_win_prob_target=0.66, min_win_prob_floor=0.68)


def test_refuses_fewer_than_100_matching_trades():
    with pytest.raises(ConfigError):
        RiskConfig(min_signature_trades=99)


@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_per_trade": 0.02},
        {"risk_per_trade": 0},
        {"max_position_pct": 0.25},
        {"max_open_positions": 7},
        {"max_per_sector": 3},
        {"cash_reserve_pct": 0.01},
        {"daily_loss_kill_pct": 0.05},
        {"drawdown_kill_pct": 0.15},
        {"slippage_bps_round_trip": 5},
    ],
)
def test_risk_limits_can_only_be_tightened(overrides):
    with pytest.raises(ConfigError):
        RiskConfig(**overrides)


def test_tighter_risk_limits_are_accepted():
    r = RiskConfig(risk_per_trade=0.005, max_open_positions=4, cash_reserve_pct=0.05)
    assert r.risk_per_trade == 0.005


# ---------------------------------------------------------------- Settings.from_env


def test_from_env_defaults_to_paper_and_dry_run():
    s = Settings.from_env({})
    assert s.paper is True
    assert s.dry_run is True


@pytest.mark.parametrize("value", ["false", "0", "no", "FALSE"])
def test_live_trading_is_refused(value):
    with pytest.raises(ConfigError, match="paper"):
        Settings.from_env({"PAPER": value})


def test_universe_defaults_to_sp100_and_sp500_is_opt_in():
    assert Settings.from_env({}).universe_name == "sp100"
    assert Settings.from_env({"UNIVERSE": "SP500"}).universe_name == "sp500"
    with pytest.raises(ConfigError, match="UNIVERSE"):
        Settings.from_env({"UNIVERSE": "russell2000"})


def test_paper_trial_is_off_by_default_and_opt_in():
    assert Settings.from_env({}).paper_trial is False
    assert Settings.from_env({"PAPER_TRIAL": "true"}).paper_trial is True
    with pytest.raises(ConfigError, match="paper"):
        Settings.from_env({"PAPER_TRIAL": "true", "PAPER": "false"})


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("False", False)])
def test_dry_run_parsing(raw, expected):
    assert Settings.from_env({"DRY_RUN": raw}).dry_run is expected


def test_unparseable_boolean_is_an_error():
    with pytest.raises(ConfigError):
        Settings.from_env({"DRY_RUN": "maybe"})


def test_secrets_are_read_but_never_shown_in_repr():
    s = Settings.from_env({"ALPACA_API_KEY": "key-123", "ALPACA_SECRET_KEY": "sec-456"})
    assert s.alpaca_api_key == "key-123"
    assert s.alpaca_secret_key == "sec-456"
    text = repr(s)
    assert "key-123" not in text and "sec-456" not in text


def test_settings_carry_validated_risk_and_strategy_config():
    s = Settings.from_env({})
    assert isinstance(s.risk, RiskConfig)
    assert isinstance(s.strategy, StrategyConfig)


def test_strategy_signature_and_exit_defaults():
    st = StrategyConfig()
    assert st.trail_ema_buffer_atr == 0.1
    assert st.partial_profit_fraction == 0.5
    assert st.avg_volume_days == 20
    assert st.spy_vol_edges == (0.01, 0.02)
    assert st.trend_strength_edges == (0.02, 0.05)
    assert st.rsi_depth_edges == (5.0, 10.0)
    assert st.ema_distance_edges == (0.5, 1.5)


@pytest.mark.parametrize(
    "overrides",
    [
        {"spy_vol_edges": (0.02, 0.01)},
        {"trend_strength_edges": (0.05,)},
        {"rsi_depth_edges": (10.0, 10.0)},
        {"ema_distance_edges": (0.5, 1.5, 2.0)},
        {"partial_profit_fraction": 0.0},
        {"partial_profit_fraction": 1.0},
        {"trail_ema_buffer_atr": -0.1},
    ],
)
def test_bad_strategy_parameters_refused(overrides):
    with pytest.raises(ConfigError):
        StrategyConfig(**overrides)


def test_load_env_file(tmp_path):
    from bot.config import load_env_file

    p = tmp_path / ".env"
    p.write_text(
        '# comment\nALPACA_API_KEY=abc\nPAPER = true\nSEC_USER_AGENT="Bot me@x.com"\n'
        "EMPTY=\nexport DRY_RUN='false'\nnot a line\n"
    )
    assert load_env_file(p) == {
        "ALPACA_API_KEY": "abc",
        "PAPER": "true",
        "SEC_USER_AGENT": "Bot me@x.com",
        "EMPTY": "",
        "DRY_RUN": "false",
    }
    assert load_env_file(tmp_path / "missing") == {}


def test_integration_settings_from_env(tmp_path):
    env = {
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/x",
        "SLACK_BOT_TOKEN": "xoxb-1",
        "SLACK_CHANNEL": "#bots",
        "CLAUDE_USAGE_SNAPSHOT": "~/usage.json",
        "CLAUDE_ROUTINE_DAILY_URL": "https://api.example/fire/d",
        "CLAUDE_ROUTINE_DAILY_TOKEN": "tok-d",
        "CLAUDE_ROUTINE_WEEKLY_URL": "https://api.example/fire/w",
        "CLAUDE_ROUTINE_WEEKLY_TOKEN": "tok-w",
        "SEC_USER_AGENT": "Bot me@example.com",
        "EARNINGS_POLICY": "strict",
        "ALTDATA_FEATURES": "news_shock_veto, insider_cluster_buy_90d",
        "BOT_DB": str(tmp_path / "bot.db"),
    }
    s = Settings.from_env(env)
    assert s.slack_webhook_url.endswith("/x")
    assert s.slack_bot_token == "xoxb-1"
    assert s.slack_channel == "#bots"
    assert s.council_slack_channel == ""
    assert Settings.from_env({"COUNCIL_SLACK_CHANNEL": "#c"}).council_slack_channel == "#c"
    assert str(s.claude_usage_snapshot).endswith("usage.json")
    assert "~" not in str(s.claude_usage_snapshot)
    assert s.routine_daily_url.endswith("/d") and s.routine_daily_token == "tok-d"
    assert s.routine_weekly_url.endswith("/w") and s.routine_weekly_token == "tok-w"
    assert s.sec_user_agent == "Bot me@example.com"
    assert s.earnings_policy == "strict"
    assert s.altdata_features == frozenset({"news_shock_veto", "insider_cluster_buy_90d"})
    assert s.db_path == tmp_path / "bot.db"
    text = repr(s)
    for secret in ("xoxb-1", "tok-d", "tok-w", "hooks.slack.com"):
        assert secret not in text


def test_integration_settings_defaults():
    s = Settings.from_env({})
    assert s.slack_channel == "#trading-bot"
    assert s.earnings_policy == "flag"  # docs/03: earnings source TBD, flag in live
    assert s.altdata_features == frozenset()
    assert s.db_path == Path("data/bot.db")
    assert s.live_data_feed == "iex"


@pytest.mark.parametrize(
    "env",
    [{"EARNINGS_POLICY": "ignore"}, {"ALTDATA_FEATURES": "options_flow"}],
)
def test_bad_integration_settings_refused(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_risk_can_be_tightened_from_env():
    s = Settings.from_env(
        {"RISK_PER_TRADE": "0.0025", "MAX_POSITION_PCT": "0.05", "MAX_OPEN_POSITIONS": "3"}
    )
    assert s.risk.risk_per_trade == 0.0025
    assert s.risk.max_position_pct == 0.05
    assert s.risk.max_open_positions == 3
    assert s.risk.min_win_prob_floor == 0.60  # untouched


@pytest.mark.parametrize(
    "env",
    [
        {"RISK_PER_TRADE": "0.02"},  # looser than the 1% ceiling
        {"MAX_POSITION_PCT": "0.5"},
        {"MAX_OPEN_POSITIONS": "10"},
        {"RISK_PER_TRADE": "abc"},
        {"MAX_OPEN_POSITIONS": "2.5"},
    ],
)
def test_env_cannot_loosen_or_garble_risk(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_blank_risk_env_keeps_defaults():
    assert Settings.from_env({"RISK_PER_TRADE": ""}).risk == RiskConfig()


def test_sentiment_exit_is_off_by_default_and_opt_in():
    s = Settings.from_env({})
    assert s.sentiment_exit is False and s.sentiment_max_age_min == 90
    s = Settings.from_env({"SENTIMENT_EXIT": "true", "SENTIMENT_MAX_AGE_MIN": "30"})
    assert s.sentiment_exit is True and s.sentiment_max_age_min == 30


def test_sentiment_max_age_must_be_a_positive_number():
    with pytest.raises(ConfigError):
        Settings.from_env({"SENTIMENT_MAX_AGE_MIN": "0"})
    with pytest.raises(ConfigError):
        Settings.from_env({"SENTIMENT_MAX_AGE_MIN": "soon"})
