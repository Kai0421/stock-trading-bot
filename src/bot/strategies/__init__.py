# Author: Kai0421
"""Pluggable trading strategies (docs/12-strategies.md).

``REGISTRY`` maps a strategy name to its class. The engine trades the one named by the
``STRATEGY`` setting; the backtester takes ``--strategy`` and ``python -m bot.compare``
runs several on the same data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bot.strategies.base import ExitAction, PositionState, Strategy

if TYPE_CHECKING:
    from bot.config import Settings

DEFAULT_STRATEGY = "trend_pullback"

__all__ = [
    "DEFAULT_STRATEGY",
    "ExitAction",
    "PositionState",
    "Strategy",
    "as_strategy",
    "get_strategy",
    "registry",
    "strategy_config",
    "strategy_for",
]


def registry() -> dict[str, type[Strategy]]:
    # Imported here: bot.strategy imports bot.strategies.base, so importing the concrete
    # strategies at module level would be circular.
    from bot.strategies.analyst_desk import AnalystDesk
    from bot.strategies.confluence_reversion import ConfluenceReversion
    from bot.strategies.high_momentum import HighMomentum
    from bot.strategies.ibs_reversion import IbsReversion
    from bot.strategies.kronos_rank import KronosRank
    from bot.strategies.rsi2_reversion import Rsi2Reversion
    from bot.strategies.trend_pullback import TrendPullback

    classes = (
        TrendPullback,
        Rsi2Reversion,
        AnalystDesk,
        IbsReversion,
        ConfluenceReversion,
        HighMomentum,
        KronosRank,
    )
    return {cls.name: cls for cls in classes}


def get_strategy(name: str, config: Any = None) -> Strategy:
    """A strategy instance by registry name, with default or given parameters."""
    classes = registry()
    if name not in classes:
        raise ValueError(f"unknown strategy {name!r}; available: {', '.join(sorted(classes))}")
    return classes[name](config)


def as_strategy(value: Strategy | Any) -> Strategy:
    """A ``Strategy`` as is; a Trend Pullback ``StrategyConfig`` wrapped (older call sites)."""
    if isinstance(value, Strategy):
        return value
    from bot.config import StrategyConfig

    if isinstance(value, StrategyConfig):
        return get_strategy("trend_pullback", value)
    raise TypeError(f"expected a Strategy or StrategyConfig, got {type(value).__name__}")


def strategy_config(name: str, settings: Settings) -> Any:
    """Parameters from the settings for strategies that take them (Trend Pullback's
    ``StrategyConfig``, ``kronos_rank``'s KRONOS_* values); None means the defaults."""
    if name == "trend_pullback":
        return settings.strategy
    if name == "kronos_rank":
        from bot.strategies.kronos_rank import KronosConfig

        overrides: dict[str, Any] = {}
        if settings.kronos_forecasts:
            overrides["forecast_path"] = settings.kronos_forecasts
        if settings.kronos_min_p_up is not None:
            overrides["min_p_up"] = settings.kronos_min_p_up
        if settings.kronos_min_fret is not None:
            overrides["min_fret"] = settings.kronos_min_fret
        return KronosConfig(**overrides) if overrides else None
    return None


def strategy_for(settings: Settings) -> Strategy:
    """The strategy the engine trades, with its parameters from ``strategy_config``; other
    strategies use their own defaults (change them in code, with a memory/decisions.md
    entry)."""
    return get_strategy(settings.strategy_name, strategy_config(settings.strategy_name, settings))
