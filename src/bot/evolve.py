# Author: Kai0421
"""Generational walk-forward tuning: each generation must beat the last on unseen data.

A strategy "improves itself" here only in a way the research supports
(docs/15-evolution.md): parameters are chosen on the past and judged on the future.

For each test year ``Y`` (``first_test_year`` .. ``last_test_year``):

1. **Champion.** Generation 0 is the strategy's default parameters. The champion in force
   during ``Y`` was decided with data from before ``Y`` only.
2. **Challengers.** Each ``stability_params`` field nudged by +-``step`` (the same +-20% the
   stability check uses) plus ``challengers`` random combinations (seeded).
3. **Train.** Every candidate is scored on the signal trades (every setup, ungated) that
   exited more than ``embargo_days`` before ``Y`` starts. The score is the conservative
   statistic of the active gate: the 95% Wilson lower bound of the win rate
   (``GATE_MODE=win_rate``, with non-positive expectancy ranked last) or the 95% lower
   bound of mean R (``GATE_MODE=expectancy``). The best one is the challenger.
4. **Test.** Champion and challenger are scored on the trades they entered during ``Y``,
   which neither selection step saw.
5. **Promote.** The challenger becomes the champion for ``Y + 1`` only if it has at least
   ``min_test_trades`` test trades and beats the champion's test score by ``margin``.

The report chains the years together and compares three ways of trading the same unseen
years: the static defaults, plain re-optimisation (always trade the newest challenger)
and the champion lineage. It counts every configuration tried, because the more variants
are tried the stronger the out-of-sample evidence must be.

Advice only: it never writes the evidence file and never changes ``STRATEGY`` or any
parameter the bot trades with. To try a lineage's final champion, backtest it with
``python -m bot.backtest --strategy NAME --set k=v ... --stability`` and record a
memory/decisions.md entry.

Run: ``uv run python -m bot.evolve --start 2016-01-01 --end 2026-09-30 --strategies
ibs_reversion,confluence_reversion``. Reports go to ``memory/backtests/evolve-*.md``.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bot.backtest import (
    DEFAULT_REPORT_DIR,
    ET,
    Trade,
    _fmt,
    fetch_start,
    simulate_signals,
    write_report,
)
from bot.config import ConfigError, RiskConfig, Settings, load_env_file
from bot.risk import mean_r_lower_bound, wilson_lower_bound
from bot.strategies import Strategy, get_strategy, registry, strategy_config
from bot.universe import BENCHMARK


@dataclass(frozen=True)
class EvolveConfig:
    first_test_year: int = 2019
    last_test_year: int = 2026
    step: float = 0.2  # +-20%, as in the stability check
    challengers: int = 8  # random multi-parameter challengers per generation
    min_train_trades: int = 100
    min_test_trades: int = 30
    margin: float = 0.0  # test-score improvement a challenger needs to be promoted
    embargo_days: int = 10  # calendar days between the training data and the test year
    seed: int = 7

    def __post_init__(self) -> None:
        if self.first_test_year > self.last_test_year:
            raise ValueError("first_test_year must not be after last_test_year")
        if not 0 < self.step < 1 or self.challengers < 0 or self.embargo_days < 0:
            raise ValueError("need 0 < step < 1, challengers >= 0 and embargo_days >= 0")
        if self.min_train_trades < 1 or self.min_test_trades < 1 or self.margin < 0:
            raise ValueError("min_train_trades, min_test_trades >= 1 and margin >= 0")


@dataclass(frozen=True)
class Score:
    n: int
    win_rate: float
    wilson_lb: float
    mean_r: float
    mean_r_lb: float
    value: float  # what candidates are ranked by (higher is better)


def fitness(trades: Sequence[Trade], risk: RiskConfig) -> Score:
    """The active gate's conservative statistic over ``trades`` (-inf when empty)."""
    n = len(trades)
    if n == 0:
        return Score(0, math.nan, math.nan, math.nan, math.nan, -math.inf)
    wins = sum(t.win for t in trades)
    r = np.array([t.r_multiple for t in trades], dtype="float64")
    mean = float(r.mean())
    sd = float(r.std(ddof=1)) if n > 1 else math.nan
    w_lb = wilson_lower_bound(wins, n)
    r_lb = mean_r_lower_bound(mean, sd, n)
    if risk.gate_mode == "expectancy":
        value = r_lb if math.isfinite(r_lb) else -math.inf
    else:
        # The win-rate gate also requires positive expectancy: rank losers last.
        value = w_lb if mean > 0 else w_lb - 1.0
    return Score(n, wins / n, w_lb, mean, r_lb, value)


def promote(champion: Score, challenger: Score, cfg: EvolveConfig) -> bool:
    """A challenger replaces the champion only with enough unseen trades and a clear win."""
    if challenger.n < cfg.min_test_trades:
        return False
    return challenger.value > champion.value + cfg.margin


def training_trades(trades: Sequence[Trade], year: int, embargo_days: int) -> list[Trade]:
    """Trades that exited more than ``embargo_days`` before ``year`` starts."""
    cutoff = pd.Timestamp(year, 1, 1) - pd.Timedelta(days=embargo_days)
    return [t for t in trades if t.exit_date < cutoff]


def holdout_trades(trades: Sequence[Trade], year: int) -> list[Trade]:
    """Trades entered during ``year``: the unseen test set."""
    return [t for t in trades if t.entry_date.year == year]


def _nudge(value: Any, factor: float) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, int):
        new = round(value * factor)
        if new == value:
            new = value + (1 if factor > 1 else -1)
        return new
    return value * factor


def _variant(base: Strategy, changes: Mapping[str, Any]) -> Strategy | None:
    if not changes or any(v is None for v in changes.values()):
        return None
    try:
        return base.with_config(dataclasses.replace(base.config, **changes))
    except (ConfigError, ValueError, TypeError):
        return None  # an invalid combination (e.g. a stop multiple of 0) is skipped


def mutations(base: Strategy, cfg: EvolveConfig, rng: np.random.Generator) -> list[Strategy]:
    """One-parameter nudges of +-``step``, then ``cfg.challengers`` random combinations,
    deduplicated and validated; never ``base`` itself."""
    params = [p for p in base.stability_params if hasattr(base.config, p)]
    factors = (1 - cfg.step, 1 + cfg.step)
    out: list[Strategy] = []
    seen = {base.config}

    def add(s: Strategy | None) -> None:
        if s is not None and s.config not in seen:
            seen.add(s.config)
            out.append(s)

    for p in params:
        for f in factors:
            add(_variant(base, {p: _nudge(getattr(base.config, p), f)}))
    for _ in range(cfg.challengers):
        changes = {}
        for p in params:
            choice = rng.integers(0, 3)  # down, unchanged, up
            if choice != 1:
                changes[p] = _nudge(getattr(base.config, p), factors[0 if choice == 0 else 1])
        add(_variant(base, changes))
    return out


class TradeCache:
    """Signal trades per parameter set, computed once on the full bar history."""

    def __init__(self, bars: Mapping[str, pd.DataFrame], risk: RiskConfig) -> None:
        self.bars = bars
        self.risk = risk
        self._cache: dict[Any, list[Trade]] = {}

    def __len__(self) -> int:
        return len(self._cache)

    def trades(self, strategy: Strategy) -> list[Trade]:
        key = (strategy.name, strategy.config)
        if key not in self._cache:
            features, tables = strategy.prepare(self.bars, BENCHMARK)
            self._cache[key] = sorted(
                (
                    t
                    for s in features
                    for t in simulate_signals(s, features[s], tables[s], strategy, self.risk)
                    if t.exit_reason != "end_of_data"
                ),
                key=lambda t: (t.exit_date, t.symbol),
            )
        return self._cache[key]


@dataclass(frozen=True)
class Generation:
    year: int
    champion: Any  # parameter dataclass in force during ``year``
    challenger: Any  # best on training data (the champion itself when nothing beat it)
    champion_train: Score
    challenger_train: Score
    champion_test: Score
    challenger_test: Score
    promoted: bool


@dataclass
class Evolution:
    strategy: str
    generations: list[Generation]
    trials: int  # distinct parameter sets evaluated
    static_trades: list[Trade] = field(default_factory=list)
    reopt_trades: list[Trade] = field(default_factory=list)
    lineage_trades: list[Trade] = field(default_factory=list)
    final_champion: Any = None
    defaults: Any = None


def evolve(
    bars: Mapping[str, pd.DataFrame],
    strategy: Strategy,
    risk: RiskConfig,
    cfg: EvolveConfig,
    cache: TradeCache | None = None,
) -> Evolution:
    """Run the generations for one strategy (module docstring)."""
    cache = cache or TradeCache(bars, risk)
    defaults = strategy
    champion = strategy
    generations: list[Generation] = []
    static, reopt, lineage = [], [], []
    tried = {strategy.config}
    for year in range(cfg.first_test_year, cfg.last_test_year + 1):
        rng = np.random.default_rng([cfg.seed, year])
        candidates = [champion, *mutations(champion, cfg, rng)]
        tried |= {c.config for c in candidates}
        scored = []
        for c in candidates:
            train = fitness(training_trades(cache.trades(c), year, cfg.embargo_days), risk)
            if train.n >= cfg.min_train_trades:
                scored.append((train.value, -len(scored), c, train))
        champ_train = fitness(training_trades(cache.trades(champion), year, cfg.embargo_days), risk)
        if scored:
            _, _, challenger, chal_train = max(scored, key=lambda x: (x[0], x[1]))
        else:
            challenger, chal_train = champion, champ_train
        champ_year = holdout_trades(cache.trades(champion), year)
        chal_year = holdout_trades(cache.trades(challenger), year)
        champ_test, chal_test = fitness(champ_year, risk), fitness(chal_year, risk)
        promoted = challenger is not champion and promote(champ_test, chal_test, cfg)
        generations.append(
            Generation(
                year,
                champion.config,
                challenger.config,
                champ_train,
                chal_train,
                champ_test,
                chal_test,
                promoted,
            )
        )
        static += holdout_trades(cache.trades(defaults), year)
        reopt += chal_year
        lineage += champ_year
        if promoted:
            champion = challenger
    return Evolution(
        strategy.name,
        generations,
        len(tried),
        static,
        reopt,
        lineage,
        champion.config,
        defaults.config,
    )


# --------------------------------------------------------- report


def _t_stat(trades: Sequence[Trade]) -> float:
    r = np.array([t.r_multiple for t in trades], dtype="float64")
    if len(r) < 2 or r.std(ddof=1) == 0:
        return math.nan
    return float(r.mean() / r.std(ddof=1) * math.sqrt(len(r)))


def _changes(old: Any, new: Any) -> str:
    diffs = [
        f"{f.name} {getattr(old, f.name):g}→{getattr(new, f.name):g}"
        for f in dataclasses.fields(old)
        if getattr(old, f.name) != getattr(new, f.name)
        and isinstance(getattr(new, f.name), int | float)
    ]
    return ", ".join(diffs) or "none"


def _score_cells(s: Score) -> str:
    return f"{s.n} | {_fmt(s.win_rate)} | {_fmt(s.mean_r, 'r')}"


def render_evolution(
    results: Sequence[Evolution],
    risk: RiskConfig,
    cfg: EvolveConfig,
    run_id: str,
    history_from: date,
    notes: Sequence[str] = (),
) -> str:
    score_name = (
        "95% lower bound of mean R"
        if risk.gate_mode == "expectancy"
        else "95% Wilson lower bound of the win rate (non-positive expectancy ranked last)"
    )
    lines = [
        f"# Strategy evolution {run_id}",
        "",
        f"- Test years: {cfg.first_test_year} to {cfg.last_test_year}; bars from "
        f"{history_from}; GATE_MODE={risk.gate_mode}",
        f"- Score: {score_name}, on signal trades (every setup, ungated, "
        f"{risk.slippage_bps_round_trip:g} bps round-trip slippage)",
        f"- Each generation: champion + one-parameter ±{cfg.step:.0%} nudges + "
        f"{cfg.challengers} random challengers; training needs ≥ {cfg.min_train_trades} "
        f"trades and ends {cfg.embargo_days} days before the test year; promotion needs "
        f"≥ {cfg.min_test_trades} test trades and a test score above the champion's "
        f"(+{cfg.margin:g}).",
        "- This report never changes what the bot trades. A lineage that beats the static "
        "defaults is a hypothesis: backtest its final champion with `--stability` and "
        "record a memory/decisions.md entry before trading it.",
        "",
    ]
    for res in results:
        lines += [
            f"## `{res.strategy}`",
            "",
            "### Same unseen years, three ways",
            "",
            "| Approach | Trades | Win rate | Lower bound | Mean R | Mean R lower bound | t-stat |",
            "|---|---|---|---|---|---|---|",
        ]
        for label, trades in (
            ("Static defaults", res.static_trades),
            ("Re-optimise every year", res.reopt_trades),
            ("Champion lineage", res.lineage_trades),
        ):
            s = fitness(trades, risk)
            lines.append(
                f"| {label} | {s.n} | {_fmt(s.win_rate)} | {_fmt(s.wilson_lb)} "
                f"| {_fmt(s.mean_r, 'r')} | {_fmt(s.mean_r_lb, 'r')} "
                f"| {_fmt(_t_stat(trades), 'num')} |"
            )
        lines += [
            "",
            f"{res.trials} distinct parameter sets were tried. With that many trials a "
            "t-stat below 3 is weak evidence (Harvey, Liu and Zhu 2016).",
            "",
            "### Generations",
            "",
            "| Year | Champion test: trades \\| win \\| mean R | Challenger test: trades \\| "
            "win \\| mean R | Promoted | Challenger changes |",
            "|---|---|---|---|---|",
        ]
        for g in res.generations:
            lines.append(
                f"| {g.year} | {_score_cells(g.champion_test)} | "
                f"{_score_cells(g.challenger_test)} | {'yes' if g.promoted else 'no'} "
                f"| {_changes(g.champion, g.challenger)} |"
            )
        lines += [
            "",
            f"Final champion vs defaults: {_changes(res.defaults, res.final_champion)}",
            "",
            "```json",
            json.dumps(dataclasses.asdict(res.final_champion), indent=2),
            "```",
            "",
        ]
        overrides = [
            f"--set {f.name}={getattr(res.final_champion, f.name)}"
            for f in dataclasses.fields(res.defaults)
            if getattr(res.final_champion, f.name) != getattr(res.defaults, f.name)
        ]
        if overrides:
            lines += [
                "To backtest it: `python -m bot.backtest --strategy "
                f"{res.strategy} {' '.join(overrides)} --stability`",
                "",
            ]
    lines += [
        "## Caveats",
        "",
        "- Survivorship bias: the universe is today's index members. Results are likely "
        "optimistic.",
        "- Scores use every setup, not the gated portfolio; the portfolio backtest is the "
        "final check.",
        "- A probability is a historical estimate, not a guarantee.",
    ]
    if notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"


def evolve_id(names: Sequence[str], risk: RiskConfig, cfg: EvolveConfig, today: date) -> str:
    payload = json.dumps(
        [sorted(names), dataclasses.asdict(risk), dataclasses.asdict(cfg)],
        default=str,
        sort_keys=True,
    )
    return f"evolve-{today:%Y%m%d}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    from bot.data import BarCache, load_daily_bars, make_data_client
    from bot.universe import UNIVERSE_NAMES, universe_for

    ap = argparse.ArgumentParser(prog="python -m bot.evolve", description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, required=True, help="first bar used")
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument(
        "--strategies",
        help="comma-separated registry names (default: STRATEGY_CANDIDATES, else all: "
        f"{', '.join(sorted(registry()))})",
    )
    ap.add_argument(
        "--first-test-year", type=int, help="first unseen year (default: start year + 3)"
    )
    ap.add_argument("--challengers", type=int, default=EvolveConfig.challengers)
    ap.add_argument("--margin", type=float, default=EvolveConfig.margin)
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--symbols", help="comma-separated subset of the universe")
    ap.add_argument(
        "--universe",
        choices=sorted(UNIVERSE_NAMES),
        help="symbol universe (default: the UNIVERSE setting, sp100)",
    )
    ap.add_argument("--cache", default="data/bars")
    ap.add_argument("--out", default=str(DEFAULT_REPORT_DIR))
    args = ap.parse_args(argv)

    if env is None:
        env = {**load_env_file(Path(".env")), **os.environ}
    try:
        first_test = args.first_test_year or args.start.year + 3
        if first_test <= args.start.year:
            raise ValueError("--first-test-year must be after the start year (training data)")
        cfg = EvolveConfig(
            first_test_year=first_test,
            last_test_year=args.end.year,
            challengers=args.challengers,
            margin=args.margin,
        )
        settings = Settings.from_env(env)
        if args.strategies:
            names = [n.strip() for n in args.strategies.split(",")]
        else:
            names = list(settings.strategy_candidates) or sorted(registry())
        strategies = [get_strategy(n, strategy_config(n, settings)) for n in names]
        first_bar = fetch_start(args.start, None)
        client = make_data_client(settings)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    universe_name = args.universe or settings.universe_name
    members = universe_for(universe_name)
    universe = args.symbols.split(",") if args.symbols else sorted(members)
    if BENCHMARK not in universe:
        universe = [BENCHMARK, *universe]
    loaded = load_daily_bars(
        client, universe, first_bar, args.end, BarCache(args.cache), feed=args.feed
    )
    if BENCHMARK not in loaded.bars:
        print(f"error: no {BENCHMARK} bars: {loaded.skipped.get(BENCHMARK)}", file=sys.stderr)
        return 2

    risk = settings.risk
    cache = TradeCache(loaded.bars, risk)
    results = []
    for s in strategies:
        res = evolve(loaded.bars, s, risk, cfg, cache)
        results.append(res)
        lineage, static = fitness(res.lineage_trades, risk), fitness(res.static_trades, risk)
        print(
            f"{s.name}: lineage {lineage.n} trades, mean {_fmt(lineage.mean_r, 'r')}, "
            f"win {_fmt(lineage.win_rate)} | static {static.n} trades, mean "
            f"{_fmt(static.mean_r, 'r')}, win {_fmt(static.win_rate)} | "
            f"{sum(g.promoted for g in res.generations)} promotions, {res.trials} trials"
        )
    notes = [f"skipped {s}: {why}" for s, why in sorted(loaded.skipped.items())]
    notes.append(f"{len(loaded.bars)} symbols, {args.feed.upper()} feed")
    run_id = evolve_id(names, risk, cfg, datetime.now(ET).date())
    if universe_name != "sp100":
        run_id += f"-{universe_name}"
    report = render_evolution(results, risk, cfg, run_id, first_bar, notes)
    path = write_report(report, run_id, args.out)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
