# Author: Kai0421
"""Configuration: risk limits, strategy parameters and environment settings.

Defaults come from docs/03-strategy.md and AGENTS.md. Risk limits are ceilings: they
can be tightened but never loosened, and the win-probability floor can't go below
``HARD_MIN_WIN_PROB_FLOOR``. The owner lowered it from 65% to 62% (target 70% -> 65%) on
2026-10-04 for a paper-trading experiment, and to 60% (target 60%) on 2026-10-07; see
memory/decisions.md. Invalid configuration
raises ``ConfigError`` so the bot refuses to start.

Strategy parameters are starting hypotheses; changes need an entry in
memory/decisions.md.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

HARD_MIN_WIN_PROB_FLOOR: Final = 0.60
MIN_SIGNATURE_TRADES: Final = 100

# Gate modes (AGENTS.md, memory/decisions.md 2026-10-05). "win_rate" is the default.
# "expectancy" is the owner's alternative for strategies that win less often but earn
# more: it drops the win-rate test, requires the 95% lower bound of the mean R to be
# positive, halves the risk per trade and caps the backtest drawdown at 6%.
GATE_MODES: Final = frozenset({"win_rate", "expectancy"})
EXPECTANCY_MAX_RISK_PER_TRADE: Final = 0.005
EXPECTANCY_MAX_BACKTEST_DRAWDOWN: Final = 0.06

# Trading modes (docs/17-intraday.md). "swing" (default) acts on daily bars and holds
# positions for days; "intraday" trades 5-minute bars and is flat every night. Both use
# the same risk limits, gate and kill switches. "overnight" buys at the close and sells
# at the next open (docs/19-overnight-hold.md): paper only, owner's waiver of the
# win-rate gate (memory/decisions.md 2026-10-08); cash checks, stops and kill switches
# still apply.
TRADING_MODES: Final = frozenset({"swing", "intraday", "overnight"})
OVERNIGHT_STRATEGY: Final = "overnight_hold"
# Pattern-day-trader rule: below this equity a margin-type account is limited to three
# day trades in five sessions, so intraday mode refuses new entries under it.
INTRADAY_MIN_EQUITY: Final = 25_000.0

# Documented limits; config may be stricter, never looser.
MAX_RISK_PER_TRADE: Final = 0.01
MAX_POSITION_PCT: Final = 0.20
MAX_OPEN_POSITIONS: Final = 6
MAX_PER_SECTOR: Final = 2
MIN_CASH_RESERVE_PCT: Final = 0.02
MAX_DAILY_LOSS_KILL_PCT: Final = 0.03
MAX_DRAWDOWN_KILL_PCT: Final = 0.10
MIN_SLIPPAGE_BPS_ROUND_TRIP: Final = 10


class ConfigError(ValueError):
    """Configuration would weaken a hard rule or is malformed. The bot must not start."""


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.01
    max_open_positions: int = 6
    max_per_sector: int = 2
    max_position_pct: float = 0.20
    cash_reserve_pct: float = 0.02
    min_win_prob_target: float = 0.60  # owner, 2026-10-07 (was 0.65); equals the floor
    min_win_prob_floor: float = 0.60  # owner, 2026-10-07 (was 0.62)
    min_signature_trades: int = 100
    daily_loss_kill_pct: float = 0.03
    drawdown_kill_pct: float = 0.10
    slippage_bps_round_trip: float = 10
    gate_mode: str = "win_rate"

    def __post_init__(self) -> None:
        if self.gate_mode not in GATE_MODES:
            raise ConfigError(f"GATE_MODE must be one of {sorted(GATE_MODES)}")
        if self.min_win_prob_floor < HARD_MIN_WIN_PROB_FLOOR:
            raise ConfigError(
                f"min_win_prob_floor {self.min_win_prob_floor} is below the hard floor "
                f"{HARD_MIN_WIN_PROB_FLOOR}; only the owner can change it (reviewed commit)"
            )
        if not self.min_win_prob_floor <= self.min_win_prob_target <= 1:
            raise ConfigError("min_win_prob_target must be between the floor and 1")
        if self.min_signature_trades < MIN_SIGNATURE_TRADES:
            raise ConfigError(f"min_signature_trades must be >= {MIN_SIGNATURE_TRADES}")
        _ceiling("risk_per_trade", self.risk_per_trade, MAX_RISK_PER_TRADE)
        _ceiling("max_position_pct", self.max_position_pct, MAX_POSITION_PCT)
        _ceiling("max_open_positions", self.max_open_positions, MAX_OPEN_POSITIONS)
        _ceiling("max_per_sector", self.max_per_sector, MAX_PER_SECTOR)
        _ceiling("daily_loss_kill_pct", self.daily_loss_kill_pct, MAX_DAILY_LOSS_KILL_PCT)
        _ceiling("drawdown_kill_pct", self.drawdown_kill_pct, MAX_DRAWDOWN_KILL_PCT)
        if not MIN_CASH_RESERVE_PCT <= self.cash_reserve_pct < 1:
            raise ConfigError(f"cash_reserve_pct must be >= {MIN_CASH_RESERVE_PCT}")
        if self.slippage_bps_round_trip < MIN_SLIPPAGE_BPS_ROUND_TRIP:
            raise ConfigError(
                f"slippage_bps_round_trip must be >= {MIN_SLIPPAGE_BPS_ROUND_TRIP} "
                "(assuming lower costs makes backtests look better than reality)"
            )

    @property
    def effective_risk_per_trade(self) -> float:
        """Risk per trade used for sizing: at most 0.5% in expectancy mode."""
        if self.gate_mode == "expectancy":
            return min(self.risk_per_trade, EXPECTANCY_MAX_RISK_PER_TRADE)
        return self.risk_per_trade


def _ceiling(name: str, value: float, limit: float) -> None:
    if not 0 < value <= limit:
        raise ConfigError(f"{name} must be > 0 and <= {limit} (got {value})")


EXIT_SETS: Final = frozenset({"trend", "mean_reversion", "mean_reversion_full"})


@dataclass(frozen=True)
class StrategyConfig:
    """Trend Pullback parameters (docs/03-strategy.md). Hypotheses until backtested."""

    sma_fast: int = 50
    sma_slow: int = 200
    ema_pullback: int = 20
    rsi_period: int = 2
    rsi_entry_below: float = 10
    pullback_lookback_days: int = 3
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    trail_atr_mult: float = 3.0
    partial_profit_r: float = 2.0
    breakeven_at_r: float = 1.0
    time_stop_days: int = 10
    time_stop_min_r: float = 0.5
    momentum_days: int = 126
    earnings_blackout_days: int = 5
    limit_offset_pct: float = 0.002
    min_price: float = 10.0
    min_avg_dollar_volume: float = 50_000_000
    avg_volume_days: int = 20
    trail_ema_buffer_atr: float = 0.1
    partial_profit_fraction: float = 0.5
    # Exit set (docs/03): "trend" = partial at +2R, then trail; "mean_reversion" = first
    # 50% as soon as the trade is in profit and the close is back above the SMA(5), or at
    # mr_target_r, then trail the rest; "mean_reversion_full" = sell everything at that
    # first target. Candidates for a higher win rate; they must pass the gate.
    exit_set: str = "mean_reversion_full"  # paper experiment (decisions.md 2026-10-04)
    mr_exit_sma: int = 5
    mr_target_r: float = 1.0
    # Setup-signature bucket edges (memory/decisions.md, 2026-10-04).
    spy_vol_edges: tuple[float, float] = (0.01, 0.02)  # SPY ATR / close
    trend_strength_edges: tuple[float, float] = (0.02, 0.05)  # SMA50 / SMA200 - 1
    rsi_depth_edges: tuple[float, float] = (5.0, 10.0)  # lowest RSI(2) in lookback
    ema_distance_edges: tuple[float, float] = (0.5, 1.5)  # (close - EMA20) / ATR

    def __post_init__(self) -> None:
        if not 0 < self.sma_fast < self.sma_slow:
            raise ConfigError("sma_fast must be positive and shorter than sma_slow")
        if self.stop_atr_mult <= 0:
            raise ConfigError("stop_atr_mult must be > 0: every position needs a stop")
        if not 0 < self.partial_profit_fraction < 1:
            raise ConfigError("partial_profit_fraction must be between 0 and 1")
        if self.trail_ema_buffer_atr < 0:
            raise ConfigError("trail_ema_buffer_atr must be >= 0")
        if self.exit_set not in EXIT_SETS:
            raise ConfigError(f"exit_set must be one of {sorted(EXIT_SETS)}")
        if self.mr_exit_sma < 2 or self.mr_target_r <= 0:
            raise ConfigError("mr_exit_sma must be >= 2 and mr_target_r > 0")
        for name in (
            "spy_vol_edges",
            "trend_strength_edges",
            "rsi_depth_edges",
            "ema_distance_edges",
        ):
            _check_edges(name, getattr(self, name))


def _check_edges(name: str, edges: tuple[float, ...]) -> None:
    if len(edges) != 2 or not edges[0] < edges[1]:
        raise ConfigError(f"{name} must be two strictly increasing values (got {edges})")


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _parse_positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not a whole number") from exc
    if value < 1:
        raise ConfigError(f"{key} must be >= 1 (got {value})")
    return value


def _parse_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{key}={raw!r} is not a boolean (use true/false)")


EARNINGS_POLICIES: Final = frozenset({"strict", "flag"})
# Alternative-data features (docs/10-alternative-data.md). Each is off until a backtest
# shows it keeps the win-probability floor and improves expectancy.
ALTDATA_FEATURES: Final = frozenset(
    {
        "insider_cluster_buy_90d",
        "insider_ceo_cfo_buy_30d",
        "insider_heavy_selling_30d",
        "activist_13d_60d",
        "news_shock_veto",
    }
)


@dataclass(frozen=True)
class Settings:
    paper: bool = True
    dry_run: bool = True
    alpaca_api_key: str = field(default="", repr=False)
    alpaca_secret_key: str = field(default="", repr=False)
    risk: RiskConfig = field(default_factory=RiskConfig)
    # Which registered strategy the engine trades (bot.strategies; docs/12-strategies.md).
    # ``strategy`` holds Trend Pullback's parameters.
    strategy_name: str = "trend_pullback"
    # TRADING_MODE: "swing" (daily bars, the default) or "intraday" (5-minute bars, flat
    # by the close). STRATEGY must name a strategy of the chosen mode.
    trading_mode: str = "swing"
    # Strategies bot.compare backtests and suggests from (empty = every registered one).
    # A suggestion never switches STRATEGY by itself (docs/12-strategies.md).
    strategy_candidates: tuple[str, ...] = ()
    # UNIVERSE: "sp100" (default) or "sp500" (opt-in, bot.universe). The evidence the
    # bot trades on must come from a backtest on the same universe.
    universe_name: str = "sp100"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    # Owner's paper trial (decisions.md 2026-10-05): trade a strategy whose backtest was
    # not accepted, to collect forward results. Paper only; per-signature gate, stops,
    # cash checks and kill switches are unchanged.
    paper_trial: bool = False
    # Saturday job that reruns the swing backtest before its evidence goes stale
    # (decisions.md 2026-10-24). The new verdict decides trading exactly as a manual run.
    auto_backtest: bool = True
    # Sentiment exit (opt-in, docs/20-sentiment.md): sell a held stock when a fresh Stocktwits
    # snapshot (written by the collector job) says sentiment turned sharply negative. Selling
    # only reduces risk; a stale or missing file sells nothing.
    sentiment_exit: bool = False
    sentiment_max_age_min: int = 90
    # kronos_rank (opt-in, docs/12-strategies.md): forecast cache path and entry thresholds.
    # Empty/None keeps the strategy's defaults (KronosConfig).
    kronos_forecasts: str = ""
    kronos_min_p_up: float | None = None
    kronos_min_fret: float | None = None
    # Notifications (docs/06-alerts.md). URLs and topics are secrets too.
    slack_webhook_url: str = field(default="", repr=False)
    slack_bot_token: str = field(default="", repr=False)
    slack_channel: str = "#trading-bot"
    # Council of Ricks messages only (empty = same channel as everything else).
    council_slack_channel: str = ""
    # Health checks and reports (empty = same channel as SLACK_CHANNEL).
    general_slack_channel: str = ""
    # NOTIFY_VIA_OUTBOX: the bot writes messages to a queue and a separate task sends them
    # (docs/06-alerts.md). Off by default; the direct sender is used when it is off.
    notify_via_outbox: bool = False
    notifier_outbox: Path = Path("data/notifier_outbox.db")
    # Claude usage watcher and routine triggers (docs/08-claude-pro-usage-limits.md).
    claude_usage_snapshot: Path = Path.home() / ".local/state/stocktradingbot/claude_usage.json"
    routine_daily_url: str = field(default="", repr=False)
    routine_daily_token: str = field(default="", repr=False)
    routine_weekly_url: str = field(default="", repr=False)
    routine_weekly_token: str = field(default="", repr=False)
    # Data
    sec_user_agent: str = ""
    live_data_feed: str = "iex"  # free plan: real-time quotes come from IEX
    # "flag": trade without an earnings date but flag it (docs/03); "strict": no trade.
    earnings_policy: str = "flag"
    altdata_features: frozenset[str] = frozenset()
    db_path: Path = Path("data/bot.db")

    def __post_init__(self) -> None:
        if not self.paper:
            raise ConfigError(
                "live trading is not allowed (paper trading only): PAPER must stay true "
                "until the user explicitly approves live trading"
            )
        if self.earnings_policy not in EARNINGS_POLICIES:
            raise ConfigError(f"EARNINGS_POLICY must be one of {sorted(EARNINGS_POLICIES)}")
        unknown = self.altdata_features - ALTDATA_FEATURES
        if unknown:
            raise ConfigError(f"unknown ALTDATA_FEATURES: {sorted(unknown)}")
        from bot.strategies import registry

        if self.trading_mode not in TRADING_MODES:
            raise ConfigError(f"TRADING_MODE must be one of {sorted(TRADING_MODES)}")
        if self.trading_mode == "intraday":
            from bot.intraday import intraday_registry

            if self.strategy_name not in intraday_registry():
                raise ConfigError(
                    f"TRADING_MODE=intraday needs an intraday STRATEGY, one of "
                    f"{sorted(intraday_registry())} (got {self.strategy_name!r})"
                )
        elif self.trading_mode == "overnight":
            if self.strategy_name != OVERNIGHT_STRATEGY:
                raise ConfigError(
                    f"TRADING_MODE=overnight trades only STRATEGY={OVERNIGHT_STRATEGY} "
                    f"(got {self.strategy_name!r})"
                )
        elif self.strategy_name not in registry():
            raise ConfigError(
                f"STRATEGY must be one of {sorted(registry())} (got {self.strategy_name!r})"
            )
        from bot.universe import UNIVERSE_NAMES

        if self.universe_name not in UNIVERSE_NAMES:
            raise ConfigError(f"UNIVERSE must be one of {sorted(UNIVERSE_NAMES)}")
        from bot.strategies import strategy_config

        strategy_config("kronos_rank", self)  # KronosConfig rejects bad KRONOS_* values
        unknown_candidates = [c for c in self.strategy_candidates if c not in registry()]
        if unknown_candidates:
            raise ConfigError(
                f"STRATEGY_CANDIDATES has unknown strategies {unknown_candidates}; "
                f"available: {sorted(registry())}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        snapshot = env.get("CLAUDE_USAGE_SNAPSHOT", "")
        features = {f.strip() for f in env.get("ALTDATA_FEATURES", "").split(",") if f.strip()}
        defaults = cls()
        risk_overrides: dict[str, float | int | str] = {}
        for key, name, kind in (
            ("RISK_PER_TRADE", "risk_per_trade", float),
            ("MAX_POSITION_PCT", "max_position_pct", float),
            ("MAX_OPEN_POSITIONS", "max_open_positions", int),
        ):
            raw = (env.get(key) or "").strip()
            if raw:
                try:
                    risk_overrides[name] = kind(raw)
                except ValueError as exc:
                    raise ConfigError(f"{key}={raw!r} is not a valid {kind.__name__}") from exc
        kronos: dict[str, float] = {}
        for key, name in (("KRONOS_MIN_P_UP", "min_p_up"), ("KRONOS_MIN_FRET", "min_fret")):
            raw = (env.get(key) or "").strip()
            if raw:
                try:
                    kronos[name] = float(raw)
                except ValueError as exc:
                    raise ConfigError(f"{key}={raw!r} is not a valid float") from exc
        gate_mode = (env.get("GATE_MODE") or "").strip().lower()
        if gate_mode:
            risk_overrides["gate_mode"] = gate_mode
        # RiskConfig enforces the ceilings: values can only be tightened, never loosened.
        risk = RiskConfig(**risk_overrides) if risk_overrides else RiskConfig()
        mode = (env.get("TRADING_MODE") or defaults.trading_mode).strip().lower()
        default_strategy = defaults.strategy_name
        if mode == "intraday":
            from bot.intraday import DEFAULT_INTRADAY_STRATEGY

            default_strategy = DEFAULT_INTRADAY_STRATEGY
        elif mode == "overnight":
            default_strategy = OVERNIGHT_STRATEGY
        return cls(
            risk=risk,
            trading_mode=mode,
            strategy_name=(env.get("STRATEGY") or default_strategy).strip().lower(),
            strategy_candidates=tuple(
                c.strip().lower()
                for c in env.get("STRATEGY_CANDIDATES", "").split(",")
                if c.strip()
            ),
            universe_name=(env.get("UNIVERSE") or defaults.universe_name).strip().lower(),
            paper=_parse_bool(env, "PAPER", True),
            paper_trial=_parse_bool(env, "PAPER_TRIAL", False),
            auto_backtest=_parse_bool(env, "AUTO_BACKTEST", True),
            sentiment_exit=_parse_bool(env, "SENTIMENT_EXIT", False),
            sentiment_max_age_min=_parse_positive_int(env, "SENTIMENT_MAX_AGE_MIN", 90),
            kronos_forecasts=(env.get("KRONOS_FORECASTS") or "").strip(),
            kronos_min_p_up=kronos.get("min_p_up"),
            kronos_min_fret=kronos.get("min_fret"),
            dry_run=_parse_bool(env, "DRY_RUN", True),
            alpaca_api_key=env.get("ALPACA_API_KEY", ""),
            alpaca_secret_key=env.get("ALPACA_SECRET_KEY", ""),
            slack_webhook_url=env.get("SLACK_WEBHOOK_URL", ""),
            slack_bot_token=env.get("SLACK_BOT_TOKEN", ""),
            slack_channel=env.get("SLACK_CHANNEL") or defaults.slack_channel,
            council_slack_channel=env.get("COUNCIL_SLACK_CHANNEL", ""),
            general_slack_channel=env.get("GENERAL_SLACK_CHANNEL", ""),
            notify_via_outbox=_parse_bool(env, "NOTIFY_VIA_OUTBOX", False),
            notifier_outbox=Path(env.get("NOTIFIER_OUTBOX") or defaults.notifier_outbox),
            claude_usage_snapshot=(
                Path(snapshot).expanduser() if snapshot else defaults.claude_usage_snapshot
            ),
            routine_daily_url=env.get("CLAUDE_ROUTINE_DAILY_URL", ""),
            routine_daily_token=env.get("CLAUDE_ROUTINE_DAILY_TOKEN", ""),
            routine_weekly_url=env.get("CLAUDE_ROUTINE_WEEKLY_URL", ""),
            routine_weekly_token=env.get("CLAUDE_ROUTINE_WEEKLY_TOKEN", ""),
            sec_user_agent=env.get("SEC_USER_AGENT", ""),
            live_data_feed=env.get("LIVE_DATA_FEED") or defaults.live_data_feed,
            earnings_policy=(env.get("EARNINGS_POLICY") or defaults.earnings_policy).lower(),
            altdata_features=frozenset(features),
            db_path=Path(env.get("BOT_DB") or defaults.db_path),
        )


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``.env`` file (KEY=VALUE, optional ``export`` and quotes).

    Missing file -> {}. Real environment variables should take precedence over it.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.isidentifier():
            out[key] = value
    return out
