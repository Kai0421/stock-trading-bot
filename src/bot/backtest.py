# Author: Kai0421
"""Backtester: walk-forward validation of a strategy against the success-probability gate.

Works with any ``bot.strategies.Strategy`` (Trend Pullback by default; ``--strategy`` picks
another). The fill model, gate, sizing and acceptance criteria are the same for all.

Two passes over daily bars (docs/03-strategy.md, docs/01-trading-fundamentals.md):

1. **Signal trades** (evidence): every setup on every symbol becomes a hypothetical
   trade (one at a time per symbol, no capital limits). Each records its setup
   signature and whether it made a net profit after costs.
2. **Portfolio** (what the bot would have done): a daily simulation with cash, slots,
   sectors and kill switches enforced by ``bot.risk.approve_buys``. A setup is taken
   only if signal trades with the same signature that **closed before that day** pass
   the probability gate (n >= 100, win rate >= target, Wilson lower bound >= floor, positive
   expectancy). The evidence window expands day by day, so every portfolio trade is
   out-of-sample with respect to the statistics that admitted it.

Fill model (no look-ahead, conservative):
- Signal on day t's close -> limit entry at day t+1's open (limit = open + 0.2%), filled
  at open + slippage. The strategy's stop (Trend Pullback: limit - 2 x ATR on day t) is
  fixed at order time, as a bracket order would be.
- A gap through the stop fills at the open; an intraday breach fills at the stop. Exits
  decided on a close fill at the next open. Every fill pays half the round-trip slippage.
- Sale proceeds settle T+1: they are not spent on the same day's entries.

Run: ``uv run python -m bot.backtest --start 2016-01-01 --end 2026-09-30 [--stability]
[--strategy NAME]``. Reports go to ``memory/backtests/<id>.md``. To compare strategies
side by side use ``python -m bot.compare``.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import hashlib
import json
import logging
import math
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot.config import (
    ALTDATA_FEATURES,
    EXPECTANCY_MAX_BACKTEST_DRAWDOWN,
    ConfigError,
    RiskConfig,
    Settings,
    StrategyConfig,
    load_env_file,
)
from bot.evidence import DEFAULT_EVIDENCE_PATH, Evidence, evidence_from_trades, save_evidence
from bot.risk import (
    Account,
    BuyRequest,
    GateResult,
    KillState,
    PortfolioState,
    approve_buys,
    kill_switch,
    mean_r_lower_bound,
    probability_gate,
    wilson_lower_bound,
)
from bot.strategies import (
    ExitAction,
    PositionState,
    Strategy,
    as_strategy,
    get_strategy,
    strategy_config,
)
from bot.universe import BENCHMARK

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
DEFAULT_REPORT_DIR = Path("memory/backtests")
# Indicator warm-up fetched before the start date (SMA200 + momentum need ~10 months).
WARMUP_CALENDAR_DAYS = 400


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    initial_equity: float = 100_000.0
    fees_per_order: float = 0.0  # Alpaca is commission-free; slippage is modelled separately
    gated: bool = True
    # Drawdown pause: 0 keeps it permanent (the live rule: the bot waits for the owner).
    # N > 0 restarts entries N trading days after a pause, as if the owner had reviewed
    # it, at ``resume_size`` of normal size until equity regains its pre-pause peak.
    # Measurement only: it lets a backtest see the whole period, it never changes the
    # live kill switch.
    resume_after_days: int = 0
    resume_size: float = 0.5

    def __post_init__(self) -> None:
        if self.resume_after_days < 0:
            raise ValueError("resume_after_days must be >= 0 (0 = the pause is permanent)")
        if not 0 < self.resume_size <= 1:
            raise ValueError("resume_size must be in (0, 1]")


def slippage_per_side(risk: RiskConfig) -> float:
    return risk.slippage_bps_round_trip / 2 / 10_000


# --------------------------------------------------------- trades


@dataclass(frozen=True)
class Trade:
    symbol: str
    signature: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    initial_stop: float
    shares: int
    pnl: float  # net of slippage and fees
    r_multiple: float
    exit_reason: str  # stop | end_of_data | the strategy's exit kind (e.g. trend_break)

    @property
    def win(self) -> bool:
        """Success = net profit after costs; breakeven or worse is a failure."""
        return bool(self.pnl > 0)


class OpenTrade:
    """A position being simulated bar by bar: open -> intraday low -> close."""

    def __init__(
        self,
        symbol: str,
        signature: str,
        signal_date: pd.Timestamp,
        entry_date: pd.Timestamp,
        entry_price: float,
        initial_stop: float,
        shares: int,
        slippage: float,
        fees: float = 0.0,
    ) -> None:
        if not 0 < initial_stop < entry_price or shares <= 0:
            raise ValueError("need 0 < stop < entry and shares > 0")
        self.symbol = symbol
        self.signature = signature
        self.signal_date = signal_date
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.initial_stop = initial_stop
        self.stop = initial_stop
        self.shares_initial = shares
        self.shares = shares
        self.slippage = slippage
        self.fees = fees
        self.fees_paid = fees  # entry order
        self.proceeds = 0.0
        self.days_held = 0
        self.partial_taken = False
        self.pending: ExitAction | None = None
        self.exit_reason = ""

    @property
    def closed(self) -> bool:
        return self.shares == 0

    def _sell(self, qty: int, price: float, reason: str) -> float:
        fill = price * (1 - self.slippage)
        self.shares -= qty
        self.proceeds += qty * fill
        self.fees_paid += self.fees
        if self.shares == 0:
            self.exit_reason = reason
        return qty * fill - self.fees

    def on_open(self, open_price: float) -> float:
        """Execute exits decided yesterday, then a gap through the stop. Returns net cash."""
        cash = 0.0
        if self.pending is not None and self.shares:
            action, self.pending = self.pending, None
            cash += self._sell(min(action.shares, self.shares), open_price, action.kind)
            if action.kind == "partial_profit":
                self.partial_taken = True
        if self.shares and open_price <= self.stop:
            cash += self._sell(self.shares, open_price, "stop")
        return cash

    def on_low(self, low: float) -> float:
        """Broker-side stop hit during the session. Returns net cash."""
        if self.shares and low <= self.stop:
            return self._sell(self.shares, self.stop, "stop")
        return 0.0

    def on_close(self, bar: Mapping[str, float], strategy: Strategy | StrategyConfig) -> None:
        """Decide tomorrow's exits and raise the stop for tomorrow. ``bar`` is the day's
        row of the strategy's features."""
        if self.closed:
            return
        self.days_held += 1
        pos = PositionState(
            self.symbol,
            self.entry_price,
            self.initial_stop,
            self.stop,
            self.shares,
            self.days_held,
            self.partial_taken,
        )
        actions, self.stop = as_strategy(strategy).manage(pos, bar)
        self.pending = actions[0] if actions else None

    def close_out(self, price: float) -> float:
        """Force the exit at the end of the data."""
        return self._sell(self.shares, price, "end_of_data") if self.shares else 0.0

    def to_trade(self, exit_date: pd.Timestamp) -> Trade:
        if not self.closed:
            raise ValueError("trade is still open")
        pnl = self.proceeds - self.entry_price * self.shares_initial - self.fees_paid
        risk = (self.entry_price - self.initial_stop) * self.shares_initial
        return Trade(
            symbol=self.symbol,
            signature=self.signature,
            signal_date=self.signal_date,
            entry_date=self.entry_date,
            exit_date=exit_date,
            entry_price=self.entry_price,
            initial_stop=self.initial_stop,
            shares=self.shares_initial,
            pnl=pnl,
            r_multiple=pnl / risk,
            exit_reason=self.exit_reason,
        )


def _entry_prices(
    open_price: float, atr_at_signal: float, strategy: Strategy, slip: float
) -> tuple[float, float, float] | None:
    """(limit, fill, stop) for a next-open limit entry, or None if no valid stop."""
    limit = strategy.limit_price(open_price)
    fill = min(open_price * (1 + slip), limit)
    try:
        stop = strategy.stop_for(limit, atr_at_signal)
    except ValueError:
        return None
    return (limit, fill, stop) if stop < fill else None


# --------------------------------------------------------- preparation and signal trades


def prepare(
    bars: Mapping[str, pd.DataFrame], strategy: Strategy | StrategyConfig
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Features and entry tables for every symbol; the benchmark is there for the regime."""
    return as_strategy(strategy).prepare(bars, BENCHMARK)


def apply_altdata(
    tables: Mapping[str, pd.DataFrame],
    vetoes: Mapping[str, set[date]] | None = None,
    flags: Mapping[str, set[date]] | None = None,
    flag_name: str = "",
) -> dict[str, pd.DataFrame]:
    """Apply point-in-time alt-data to entry tables (inputs are not modified).

    A setup on day t enters on the next bar, so its as-of date is that bar's date.
    ``vetoes`` remove setups; ``flags`` append ``|<flag_name>=0/1`` to the signature so
    flagged and unflagged setups are gated on separate evidence.
    """
    out = {}
    for symbol, table in tables.items():
        t = table.copy()
        next_day = list(t.index[1:]) + [t.index[-1] + pd.offsets.BDay(1)] if len(t) else []
        as_of = [d.date() for d in next_day]
        if vetoes and symbol in vetoes:
            vetoed = pd.Series([d in vetoes[symbol] for d in as_of], index=t.index)
            t["setup"] = t["setup"] & ~vetoed
        if flags is not None and flag_name:
            marks = flags.get(symbol, set())
            suffix = pd.Series([f"|{flag_name}={int(d in marks)}" for d in as_of], index=t.index)
            t["signature"] = (t["signature"].astype(str) + suffix).where(t["setup"], None)
        out[symbol] = t
    return out


def feature_dates(
    store: Any, tables: Mapping[str, pd.DataFrame], feature: str
) -> dict[str, set[date]]:
    """Entry days (the bar after each setup) on which ``feature`` was true, point in time."""
    from bot.altdata import compute_features

    out: dict[str, set[date]] = {}
    for symbol, table in tables.items():
        idx = table.index
        days = set()
        for i in [k for k, ok in enumerate(table["setup"].to_numpy(dtype=bool)) if ok]:
            as_of = (idx[i + 1] if i + 1 < len(idx) else idx[i] + pd.offsets.BDay(1)).date()
            if compute_features(store, symbol, as_of).get(feature):
                days.add(as_of)
        out[symbol] = days
    return out


def _columns(features: pd.DataFrame) -> dict[str, np.ndarray]:
    return {str(k): features[k].to_numpy() for k in features.columns}


def _bar(cols: Mapping[str, np.ndarray], i: int) -> dict[str, float]:
    return {k: v[i] for k, v in cols.items()}


def simulate_signals(
    symbol: str,
    features: pd.DataFrame,
    table: pd.DataFrame,
    strategy: Strategy | StrategyConfig,
    risk: RiskConfig,
    nominal_shares: int = 100,
    fees: float = 0.0,
) -> list[Trade]:
    """Hypothetical trade for every setup, one open trade at a time per symbol."""
    strategy = as_strategy(strategy)
    slip = slippage_per_side(risk)
    idx = features.index
    cols = _columns(features)
    o, lo, c, atr = (cols[k] for k in ("open", "low", "close", "atr"))
    setup = table["setup"].to_numpy(dtype=bool)
    sig = table["signature"].to_numpy(dtype=object)
    trades: list[Trade] = []
    open_t: OpenTrade | None = None
    pending: int | None = None

    for j in range(len(idx)):
        if open_t is not None:
            open_t.on_open(o[j])
            open_t.on_low(lo[j])
        elif pending is not None:
            prices = _entry_prices(o[j], atr[pending], strategy, slip)
            if prices is not None:
                _, fill, stop = prices
                open_t = OpenTrade(
                    symbol,
                    sig[pending],
                    idx[pending],
                    idx[j],
                    fill,
                    stop,
                    nominal_shares,
                    slip,
                    fees,
                )
                open_t.on_low(lo[j])
            pending = None
        if open_t is not None:
            if open_t.closed:
                trades.append(open_t.to_trade(idx[j]))
                open_t = None
            else:
                open_t.on_close(_bar(cols, j), strategy)
        if open_t is None and setup[j] and j + 1 < len(idx):
            pending = j

    if open_t is not None:
        open_t.close_out(c[-1])
        trades.append(open_t.to_trade(idx[-1]))
    return trades


# --------------------------------------------------------- point-in-time evidence


class Gate(Protocol):
    def gate(self, signature: str, as_of: pd.Timestamp, cfg: RiskConfig) -> GateResult: ...


class SignatureBook:
    """Per-signature trade history, queried point-in-time (only exits before ``as_of``)."""

    def __init__(self, trades: Iterable[Trade]) -> None:
        by_sig: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            if t.exit_reason != "end_of_data":
                by_sig[t.signature].append(t)
        self._exits: dict[str, list[pd.Timestamp]] = {}
        self._wins: dict[str, np.ndarray] = {}
        self._r: dict[str, np.ndarray] = {}
        self._r2: dict[str, np.ndarray] = {}
        for s, ts in by_sig.items():
            ts.sort(key=lambda t: t.exit_date)
            self._exits[s] = [t.exit_date for t in ts]
            self._wins[s] = np.cumsum([t.win for t in ts])
            self._r[s] = np.cumsum([t.r_multiple for t in ts])
            self._r2[s] = np.cumsum([t.r_multiple**2 for t in ts])

    def stats(self, signature: str, as_of: pd.Timestamp) -> tuple[int, int, float, float]:
        """(n, wins, mean R, sample SD of R) of trades with this signature that exited
        before ``as_of``."""
        exits = self._exits.get(signature)
        if not exits:
            return 0, 0, math.nan, math.nan
        n = bisect.bisect_left(exits, as_of)
        if n == 0:
            return 0, 0, math.nan, math.nan
        total, squares = float(self._r[signature][n - 1]), float(self._r2[signature][n - 1])
        mean = total / n
        sd = math.sqrt(max(0.0, (squares - n * mean * mean) / (n - 1))) if n > 1 else math.nan
        return n, int(self._wins[signature][n - 1]), mean, sd

    def gate(self, signature: str, as_of: pd.Timestamp, cfg: RiskConfig) -> GateResult:
        n, wins, mean_r, sd_r = self.stats(signature, as_of)
        if n == 0:
            return GateResult(False, "no matching trades", math.nan, math.nan)
        return probability_gate(wins, n, mean_r, cfg, sd_r)


# --------------------------------------------------------- portfolio


@dataclass(frozen=True)
class _Pending:
    symbol: str
    signature: str
    signal_date: pd.Timestamp
    atr: float
    score: float
    win_prob: float


@dataclass
class PortfolioResult:
    trades: list[Trade]
    equity: pd.Series
    invested: pd.Series
    paused_on: pd.Timestamp | None = None  # first drawdown pause
    gate_rejections: int = 0
    halted_days: int = 0
    min_cash: float = math.inf
    max_positions: int = 0
    max_per_sector: int = 0
    # (paused on, resumed on or None) for every drawdown pause
    pauses: list[tuple[pd.Timestamp, pd.Timestamp | None]] = field(default_factory=list)
    reduced_size_entries: int = 0


def run_portfolio(
    features: Mapping[str, pd.DataFrame],
    tables: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str],
    calendar: pd.DatetimeIndex,
    book: Gate | None,
    strategy: Strategy | StrategyConfig,
    risk: RiskConfig,
    bt: BacktestConfig,
) -> PortfolioResult:
    """Daily portfolio simulation. ``book=None`` takes every setup (diagnostics only)."""
    strategy = as_strategy(strategy)
    days = calendar[(calendar >= pd.Timestamp(bt.start)) & (calendar <= pd.Timestamp(bt.end))]
    slip = slippage_per_side(risk)
    whitelist = frozenset(features)
    rows = {s: f.index.get_indexer(days) for s, f in features.items()}
    cols = {s: _columns(f) for s, f in features.items()}
    scores = {s: t["score"].to_numpy(dtype="float64") for s, t in tables.items()}
    setups = {s: t["setup"].to_numpy(dtype=bool) for s, t in tables.items()}
    sigs = {s: t["signature"].to_numpy(dtype=object) for s, t in tables.items()}

    res = PortfolioResult(trades=[], equity=pd.Series(dtype="float64"), invested=pd.Series())
    cash = bt.initial_equity
    equity_prev = peak = bt.initial_equity
    halt_today = False
    positions: dict[str, OpenTrade] = {}
    last_close: dict[str, float] = {}
    pending: list[_Pending] = []
    equity_values: list[float] = []
    invested_values: list[float] = []
    paused_days = 0  # trading days into the current pause
    recover_to: float | None = None  # pre-pause peak to regain before full size

    def close_trade(sym: str, d: pd.Timestamp) -> None:
        res.trades.append(positions.pop(sym).to_trade(d))

    for k, d in enumerate(days):
        unsettled = 0.0
        for sym, t in list(positions.items()):
            r = rows[sym][k]
            if r >= 0:
                got = t.on_open(cols[sym]["open"][r])
                cash += got
                unsettled += got
                if t.closed:
                    close_trade(sym, d)

        if pending:
            requests: list[BuyRequest] = []
            meta: dict[str, tuple[float, float, _Pending]] = {}
            for p in pending:
                r = rows[p.symbol][k]
                if r < 0 or p.symbol in positions:
                    continue
                prices = _entry_prices(cols[p.symbol]["open"][r], p.atr, strategy, slip)
                if prices is None:
                    continue
                limit, fill, stop = prices
                sector = sectors.get(p.symbol, "")
                requests.append(
                    BuyRequest(p.symbol, limit, stop, sector, est_fees=bt.fees_per_order)
                )
                meta[p.symbol] = (fill, stop, p)
            paused = bool(res.pauses) and res.pauses[-1][1] is None
            kill = KillState(halt_new_orders_today=halt_today, paused=paused)
            state = PortfolioState(
                account=Account(cash, cash, equity_prev),
                margin_lock_verified=True,
                unsettled_proceeds=unsettled,
                open_buy_orders_cost=0.0,
                open_position_sectors=tuple(sectors.get(s, "") for s in positions),
                kill=kill,
                whitelist=whitelist,
            )
            for dec in approve_buys(requests, state, risk):
                if not dec.approved:
                    continue
                fill, stop, p = meta[dec.symbol]
                shares = dec.shares
                if recover_to is not None:
                    shares = math.floor(shares * bt.resume_size)
                    if shares < 1:
                        continue
                    res.reduced_size_entries += 1
                positions[dec.symbol] = OpenTrade(
                    dec.symbol, p.signature, p.signal_date, d, fill, stop, shares, slip,
                    bt.fees_per_order,
                )  # fmt: skip
                cash -= shares * fill + bt.fees_per_order
            pending = []
            res.max_positions = max(res.max_positions, len(positions))
            per_sector = Counter(sectors.get(s, "") for s in positions)
            res.max_per_sector = max([res.max_per_sector, *per_sector.values()])
        res.min_cash = min(res.min_cash, cash)

        for sym, t in list(positions.items()):
            r = rows[sym][k]
            if r >= 0:
                cash += t.on_low(cols[sym]["low"][r])
                if t.closed:
                    close_trade(sym, d)

        invested = 0.0
        for sym, t in positions.items():
            r = rows[sym][k]
            if r >= 0:
                t.on_close(_bar(cols[sym], r), strategy)
                last_close[sym] = cols[sym]["close"][r]
            invested += t.shares * last_close[sym]
        equity = cash + invested
        equity_values.append(equity)
        invested_values.append(invested)

        peak = max(peak, equity)
        if recover_to is not None and equity >= recover_to:
            recover_to = None  # back to the pre-pause peak: full size again
        ks = kill_switch(equity, equity_prev, peak, risk)
        halt_today = ks.halt_new_orders_today
        res.halted_days += int(halt_today)
        paused = bool(res.pauses) and res.pauses[-1][1] is None
        if paused:
            paused_days += 1
            if bt.resume_after_days and paused_days >= bt.resume_after_days:
                # Restart as if the owner reviewed the pause: the drawdown is measured
                # from here, and size stays reduced until the old peak is regained.
                res.pauses[-1] = (res.pauses[-1][0], d)
                recover_to = max(recover_to or 0.0, peak)
                peak = equity
                paused = False
        elif ks.paused:
            res.pauses.append((d, None))
            res.paused_on = res.paused_on or d
            paused, paused_days = True, 0
        equity_prev = equity

        if not paused:
            candidates: list[_Pending] = []
            for sym in tables:
                r = rows[sym][k]
                if sym in positions or r < 0 or not setups[sym][r]:
                    continue
                signature = sigs[sym][r]
                win_prob = 0.0
                if book is not None:
                    g = book.gate(signature, d, risk)
                    if not g.passed:
                        res.gate_rejections += 1
                        continue
                    win_prob = g.win_rate
                score = float(scores[sym][r])
                candidates.append(_Pending(sym, signature, d, cols[sym]["atr"][r], score, win_prob))
            candidates.sort(key=lambda p: (-p.win_prob, -_nan_low(p.score), p.symbol))
            pending = candidates[: max(risk.max_open_positions - len(positions), 0)]

    if len(days):
        for sym, t in list(positions.items()):
            cash += t.close_out(last_close[sym])
            close_trade(sym, days[-1])
    res.equity = pd.Series(equity_values, index=days, dtype="float64", name="equity")
    res.invested = pd.Series(invested_values, index=days, dtype="float64", name="invested")
    return res


def _nan_low(x: float) -> float:
    return -math.inf if math.isnan(x) else x


# --------------------------------------------------------- metrics


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    win_rate: float
    wilson_lb: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    exposure: float
    expectancy_lb_r: float = math.nan  # 95% lower bound of the mean R


def _mean(values: Sequence[float]) -> float:
    return float(np.mean(values)) if len(values) else math.nan


def compute_metrics(
    trades: Sequence[Trade], equity: pd.Series | None, invested: pd.Series | None = None
) -> Metrics:
    n = len(trades)
    wins = sum(t.win for t in trades)
    rs = [t.r_multiple for t in trades]
    nan = math.nan
    total_return = cagr = max_dd = sharpe = exposure = nan
    if equity is not None and len(equity) >= 2:
        total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
        years = (equity.index[-1] - equity.index[0]).days / 365.25
        if years > 0 and equity.iloc[-1] > 0:
            cagr = float((1 + total_return) ** (1 / years) - 1)
        max_dd = float((1 - equity / equity.cummax()).max())
        daily = equity.pct_change().dropna()
        std = float(daily.std())
        sharpe = float(daily.mean() / std * math.sqrt(252)) if std > 0 else 0.0
        if invested is not None and len(invested) == len(equity):
            exposure = float((invested / equity).mean())
    return Metrics(
        trades=n,
        wins=wins,
        win_rate=wins / n if n else nan,
        wilson_lb=wilson_lower_bound(wins, n) if n else nan,
        expectancy_r=_mean(rs),
        avg_win_r=_mean([t.r_multiple for t in trades if t.win]),
        avg_loss_r=_mean([t.r_multiple for t in trades if not t.win]),
        total_return=total_return,
        cagr=cagr,
        max_drawdown=max_dd,
        sharpe=sharpe,
        exposure=exposure,
        expectancy_lb_r=mean_r_lower_bound(_mean(rs), float(np.std(rs, ddof=1)), n)
        if n > 1
        else nan,
    )


MAX_BACKTEST_DRAWDOWN = 0.20


def acceptance(m: Metrics, risk: RiskConfig) -> list[str]:
    """Backtest acceptance criteria (docs/03-strategy.md). Empty list = accepted."""
    failures: list[str] = []
    if m.trades < risk.min_signature_trades:
        failures.append(f"only {m.trades} trades (need >= {risk.min_signature_trades})")
    if risk.gate_mode == "expectancy":
        if not m.expectancy_lb_r > 0:
            failures.append(
                f"95% lower bound of mean R {_fmt(m.expectancy_lb_r, 'r')} not positive"
            )
        max_dd = EXPECTANCY_MAX_BACKTEST_DRAWDOWN
    else:
        if not m.win_rate >= risk.min_win_prob_target:
            failures.append(f"win rate {_fmt(m.win_rate)} below {risk.min_win_prob_target:.0%}")
        if not m.wilson_lb >= risk.min_win_prob_floor:
            failures.append(
                f"95% lower bound {_fmt(m.wilson_lb)} below {risk.min_win_prob_floor:.0%}"
            )
        max_dd = MAX_BACKTEST_DRAWDOWN
    if not m.expectancy_r > 0:
        failures.append(f"expectancy {_fmt(m.expectancy_r, 'r')} not positive")
    if not m.max_drawdown < max_dd:
        failures.append(f"max drawdown {_fmt(m.max_drawdown)} not below {max_dd:.0%}")
    return failures


def yearly_breakdown(trades: Sequence[Trade]) -> pd.DataFrame:
    """Per-exit-year trade count, win rate and expectancy (walk-forward view)."""
    if not trades:
        return pd.DataFrame(columns=["trades", "win_rate", "expectancy_r"])
    df = pd.DataFrame(
        {"year": [t.exit_date.year for t in trades], "win": [t.win for t in trades]}
        | {"r": [t.r_multiple for t in trades]}
    )
    g = df.groupby("year")
    return pd.DataFrame(
        {"trades": g.size(), "win_rate": g["win"].mean(), "expectancy_r": g["r"].mean()}
    )


# --------------------------------------------------------- full run


@dataclass
class BacktestResult:
    signal_trades: list[Trade]
    signal_metrics: Metrics
    portfolio: PortfolioResult
    metrics: Metrics
    failures: list[str]
    yearly: pd.DataFrame
    signatures: pd.DataFrame = field(default_factory=pd.DataFrame)
    strategy: str = "trend_pullback"


def signature_table(trades: Sequence[Trade]) -> pd.DataFrame:
    """Per-signature evidence over all closed signal trades, most frequent first."""
    rows = defaultdict(list)
    for t in trades:
        if t.exit_reason != "end_of_data":
            rows[t.signature].append(t)
    out = [
        {
            "signature": s,
            "trades": len(ts),
            "win_rate": sum(t.win for t in ts) / len(ts),
            "wilson_lb": wilson_lower_bound(sum(t.win for t in ts), len(ts)),
            "expectancy_r": _mean([t.r_multiple for t in ts]),
        }
        for s, ts in rows.items()
    ]
    if not out:
        return pd.DataFrame(
            columns=["signature", "trades", "win_rate", "wilson_lb", "expectancy_r"]
        )
    return pd.DataFrame(out).sort_values(["trades", "signature"], ascending=[False, True])


def run_backtest(
    bars: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str],
    strategy: Strategy | StrategyConfig,
    risk: RiskConfig,
    bt: BacktestConfig,
    vetoes: Mapping[str, set[date]] | None = None,
    flags: Mapping[str, set[date]] | None = None,
    flag_name: str = "",
) -> BacktestResult:
    strategy = as_strategy(strategy)
    features, tables = prepare(bars, strategy)
    if vetoes or flag_name:
        tables = apply_altdata(tables, vetoes, flags, flag_name)
    signal_trades = sorted(
        (
            t
            for s in features
            for t in simulate_signals(
                s, features[s], tables[s], strategy, risk, fees=bt.fees_per_order
            )
        ),
        key=lambda t: (t.exit_date, t.symbol),
    )
    book = SignatureBook(signal_trades) if bt.gated else None
    portfolio = run_portfolio(
        features, tables, sectors, features[BENCHMARK].index, book, strategy, risk, bt
    )
    start, end = pd.Timestamp(bt.start), pd.Timestamp(bt.end)
    in_window = [t for t in signal_trades if start <= t.exit_date <= end]
    metrics = compute_metrics(portfolio.trades, portfolio.equity, portfolio.invested)
    return BacktestResult(
        signal_trades=signal_trades,
        signal_metrics=compute_metrics(in_window, None),
        portfolio=portfolio,
        metrics=metrics,
        failures=acceptance(metrics, risk),
        yearly=yearly_breakdown(portfolio.trades),
        signatures=signature_table(in_window),
        strategy=strategy.name,
    )


# --------------------------------------------------------- stability


def fetch_start(start: date, history_start: date | None) -> date:
    """First bar to load: indicator warm-up by default, or earlier so the walk-forward
    evidence for a held-out period accumulates from ``history_start``."""
    if history_start is None:
        return start - timedelta(days=WARMUP_CALENDAR_DAYS)
    if history_start >= start:
        raise ValueError("--history-start must be before --start")
    return history_start


def apply_overrides(config: Any, items: Sequence[str]) -> Any:
    """``["key=value", ...]`` -> a validated copy of a strategy's parameter dataclass."""
    fields = {f.name: f for f in dataclasses.fields(config)}
    changes: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or key not in fields:
            raise ValueError(f"bad override {item!r}: use name=value with a strategy parameter")
        current = getattr(config, key)
        try:
            if isinstance(current, bool):
                value: Any = raw.lower() in {"1", "true", "yes"}
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            elif isinstance(current, str):
                value = raw
            else:
                raise ValueError(f"{key} can't be overridden from the command line")
        except ValueError as exc:
            raise ValueError(f"bad value for {key}: {raw!r}") from exc
        changes[key] = value
    return dataclasses.replace(config, **changes)


def perturbations(
    strategy: Strategy | StrategyConfig, pct: float = 0.2, params: Sequence[str] | None = None
) -> list[tuple[str, Strategy]]:
    """Each parameter moved by -pct and +pct (ints rounded); invalid or unchanged skipped.
    ``params`` defaults to the strategy's ``stability_params``."""
    strategy = as_strategy(strategy)
    base = strategy.config
    out: list[tuple[str, Strategy]] = []
    for name in strategy.stability_params if params is None else params:
        value = getattr(base, name)
        for factor in (1 - pct, 1 + pct):
            new = factor * value
            new = round(new) if isinstance(value, int) else round(new, 10)
            if new == value:
                continue
            try:
                cfg = dataclasses.replace(base, **{name: new})
            except ConfigError:
                continue
            out.append((f"{name} x{factor:g}", strategy.with_config(cfg)))
    return out


@dataclass(frozen=True)
class StabilityResult:
    label: str
    passed: bool
    metrics: Metrics
    failures: tuple[str, ...]


def stability_check(
    bars: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str],
    strategy: Strategy | StrategyConfig,
    risk: RiskConfig,
    bt: BacktestConfig,
    pct: float = 0.2,
    params: Sequence[str] | None = None,
) -> list[StabilityResult]:
    results = []
    for label, variant in perturbations(strategy, pct, params):
        r = run_backtest(bars, sectors, variant, risk, bt)
        results.append(StabilityResult(label, not r.failures, r.metrics, tuple(r.failures)))
    return results


# --------------------------------------------------------- report


def _fmt(value: float, kind: str = "pct") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if kind == "pct":
        return f"{value:.1%}"
    if kind == "r":
        return f"{value:+.3f}R"
    return f"{value:.2f}"


def _pauses_text(p: PortfolioResult) -> str:
    """Each drawdown pause as "start to restart" ("never resumed" when permanent)."""
    if not p.pauses:
        return "none"
    return "; ".join(
        f"{a.date()} to {b.date() if b is not None else 'never resumed'}" for a, b in p.pauses
    )


def _md_cell(text: str) -> str:
    """Escape pipes so signatures don't split markdown table cells."""
    return text.replace("|", "\\|")


def _metrics_rows(m: Metrics) -> list[str]:
    return [
        f"| Trades | {m.trades} |",
        f"| Win rate | {_fmt(m.win_rate)} |",
        f"| 95% Wilson lower bound | {_fmt(m.wilson_lb)} |",
        f"| Expectancy | {_fmt(m.expectancy_r, 'r')} |",
        f"| Avg win / avg loss | {_fmt(m.avg_win_r, 'r')} / {_fmt(m.avg_loss_r, 'r')} |",
        f"| Total return | {_fmt(m.total_return)} |",
        f"| CAGR | {_fmt(m.cagr)} |",
        f"| Max drawdown | {_fmt(m.max_drawdown)} |",
        f"| Sharpe (daily, annualised) | {_fmt(m.sharpe, 'num')} |",
        f"| Exposure | {_fmt(m.exposure)} |",
    ]


def verdict(
    result: BacktestResult,
    stability: Sequence[StabilityResult] | None,
    advisory: bool = False,
) -> str:
    """ACCEPTED needs the main run and every ±20% variant to pass. ``advisory`` (paper
    experiment) reports failing variants but still accepts, with a visible label."""
    if result.failures:
        return "REJECTED"
    if stability is None:
        return "PROVISIONAL (stability check not run)"
    if all(s.passed for s in stability):
        return "ACCEPTED"
    return "ACCEPTED (stability advisory)" if advisory else "REJECTED (unstable)"


def render_report(
    result: BacktestResult,
    strategy: Strategy | StrategyConfig,
    risk: RiskConfig,
    bt: BacktestConfig,
    run_id: str,
    notes: Sequence[str] = (),
    stability: Sequence[StabilityResult] | None = None,
    advisory: bool = False,
) -> str:
    strategy = as_strategy(strategy)
    p = result.portfolio
    lines = [
        f"# Backtest {run_id}",
        "",
        f"- **Verdict: {verdict(result, stability, advisory)}**",
        f"- Strategy: `{strategy.name}`",
        f"- Period: {bt.start} to {bt.end}; initial equity ${bt.initial_equity:,.0f}",
        f"- Probability gate: {'on (walk-forward, expanding window)' if bt.gated else 'OFF'}",
        f"- Costs: {risk.slippage_bps_round_trip:g} bps round-trip slippage, "
        f"${bt.fees_per_order:g} fees per order",
        f"- Gate rejections: {p.gate_rejections}; daily-loss halts: {p.halted_days}; "
        f"drawdown pauses: {_pauses_text(p)}",
        "",
        "## Portfolio trades (out-of-sample)",
        "",
        "| Metric | Value |",
        "|---|---|",
        *_metrics_rows(result.metrics),
        "",
        "### Acceptance",
        "",
        *([f"- FAIL: {f}" for f in result.failures] or ["- All criteria met"]),
        "",
        "### Per year (exit year)",
        "",
        "| Year | Trades | Win rate | Expectancy |",
        "|---|---|---|---|",
        *[
            f"| {y} | {int(r.trades)} | {_fmt(r.win_rate)} | {_fmt(r.expectancy_r, 'r')} |"
            for y, r in result.yearly.iterrows()
        ],
        "",
        "## Signal-level evidence (every setup, ungated)",
        "",
        "| Metric | Value |",
        "|---|---|",
        *_metrics_rows(result.signal_metrics)[:5],
        "",
        "| Signature | Trades | Win rate | Lower bound | Expectancy |",
        "|---|---|---|---|---|",
        *[
            f"| `{_md_cell(r.signature)}` | {r.trades} | {_fmt(r.win_rate)} "
            f"| {_fmt(r.wilson_lb)} | {_fmt(r.expectancy_r, 'r')} |"
            for r in result.signatures.head(15).itertuples()
        ],
        "",
    ]
    if stability is not None:
        lines += [
            "## Stability (each parameter ±20%)"
            + (" — advisory only (paper experiment)" if advisory else ""),
            "",
            "| Change | Passed | Trades | Win rate | Expectancy | Max DD |",
            "|---|---|---|---|---|---|",
            *[
                f"| {s.label} | {'yes' if s.passed else 'no'} | {s.metrics.trades} "
                f"| {_fmt(s.metrics.win_rate)} | {_fmt(s.metrics.expectancy_r, 'r')} "
                f"| {_fmt(s.metrics.max_drawdown)} |"
                for s in stability
            ],
            "",
        ]
    lines += [
        "## Parameters",
        "",
        "```json",
        json.dumps(
            {
                "strategy_name": strategy.name,
                "strategy": dataclasses.asdict(strategy.config),
                "risk": dataclasses.asdict(risk),
            },
            indent=2,
        ),
        "```",
        "",
        "## Caveats",
        "",
        "- Survivorship bias: the universe is today's index members (S&P 100, or S&P 500 "
        "with `--universe sp500`), so names that left the index are missing. Results are "
        "likely optimistic.",
        "- Earnings blackout is skipped (no point-in-time earnings calendar yet).",
        "- Prices are split- and dividend-adjusted; fills are modelled from daily OHLC "
        "(next open + slippage; stops at the stop price or the gap open).",
        "- A probability is a historical estimate, not a guarantee.",
    ]
    if notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"


def write_report(text: str, run_id: str, directory: Path | str = DEFAULT_REPORT_DIR) -> Path:
    path = Path(directory) / f"{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def evidence_for(
    result: BacktestResult,
    bt: BacktestConfig,
    run_id: str,
    verdict_: str,
    universe: str = "sp100",
) -> Evidence:
    """Signal-trade evidence up to and including the backtest end date, for the live gate."""
    as_of = pd.Timestamp(bt.end) + pd.Timedelta(days=1)
    signatures = evidence_from_trades(result.signal_trades, as_of)
    return Evidence(signatures, bt.end, run_id, verdict_, result.strategy, universe)


def altdata_comparison(
    feature: str,
    bars: Mapping[str, pd.DataFrame],
    sectors: Mapping[str, str],
    settings: Settings,
    bt: BacktestConfig,
    baseline: BacktestResult,
    strategy: Strategy | None = None,
) -> list[str]:
    """Backfill one alt-data feature and compare against the baseline (docs/10 rule 2)."""
    from alpaca.data.historical.news import NewsClient

    from bot.altdata import EDGAR_FEATURES, VETO_FEATURES, AltStore, LiveSource
    from bot.altdata.edgar import EdgarClient
    from bot.journal import Journal

    store = AltStore(Journal(settings.db_path))
    edgar = EdgarClient(settings.sec_user_agent) if feature in EDGAR_FEATURES else None
    news = (
        NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        if feature == "news_shock_veto"
        else None
    )
    source = LiveSource(store, edgar, news, frozenset({feature}))
    lookback = (bt.end - bt.start).days + 120
    for symbol in bars:
        try:
            source.refresh(symbol, bt.end + timedelta(days=1), lookback_days=lookback)
        except Exception as exc:
            log.warning("alt-data backfill for %s failed: %s", symbol, exc)
    strat = strategy or as_strategy(settings.strategy)
    risk = settings.risk
    _, tables = prepare(bars, strat)
    dates = feature_dates(store, tables, feature)
    if feature in VETO_FEATURES:
        with_feature = run_backtest(bars, sectors, strat, risk, bt, vetoes=dates)
    else:
        name = feature.split("_")[1] if "_" in feature else feature
        with_feature = run_backtest(bars, sectors, strat, risk, bt, flags=dates, flag_name=name)
    b, w = baseline.metrics, with_feature.metrics
    adopt = not with_feature.failures and (w.expectancy_r or 0) > (b.expectancy_r or 0)
    return [
        f"alt-data {feature}: baseline {b.trades} trades, win {_fmt(b.win_rate)}, "
        f"expectancy {_fmt(b.expectancy_r, 'r')}; with feature {w.trades} trades, "
        f"win {_fmt(w.win_rate)}, expectancy {_fmt(w.expectancy_r, 'r')}",
        f"alt-data {feature}: {'CANDIDATE for adoption' if adopt else 'do not adopt'} "
        "(needs acceptance AND better expectancy; adoption is a reviewed decisions.md entry)",
    ]


def run_id_for(
    strategy: Strategy | StrategyConfig, risk: RiskConfig, bt: BacktestConfig, today: date
) -> str:
    strategy = as_strategy(strategy)
    config = dataclasses.asdict(strategy.config)
    if strategy.name != "trend_pullback":  # Trend Pullback ids stay as they were
        config = {"strategy_name": strategy.name, **config}
    payload = json.dumps(
        [config, dataclasses.asdict(risk), dataclasses.asdict(bt)],
        default=str,
        sort_keys=True,
    )
    return f"{today:%Y%m%d}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


# --------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    from bot.data import BarCache, load_daily_bars, make_data_client
    from bot.universe import UNIVERSE_NAMES, universe_for

    ap = argparse.ArgumentParser(prog="python -m bot.backtest", description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument(
        "--history-start",
        type=date.fromisoformat,
        help="load bars from here so gate evidence builds up before --start (held-out tests)",
    )
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--symbols", help="comma-separated subset of the universe")
    ap.add_argument(
        "--universe",
        choices=sorted(UNIVERSE_NAMES),
        help="symbol universe (default: the UNIVERSE setting, sp100)",
    )
    ap.add_argument("--no-gate", action="store_true", help="diagnostics: take every setup")
    ap.add_argument(
        "--resume-after-days",
        type=int,
        default=0,
        help="restart N trading days after a drawdown pause at half size (0 = never)",
    )
    ap.add_argument("--stability", action="store_true", help="also run the ±20%% check")
    ap.add_argument(
        "--stability-advisory",
        action="store_true",
        help="paper experiment: report failing ±20%% variants but don't reject on them",
    )
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="override a strategy parameter for this run, e.g. exit_set=mean_reversion",
    )
    ap.add_argument(
        "--strategy",
        help="registered strategy to backtest (default: the STRATEGY setting, trend_pullback)",
    )
    ap.add_argument("--cache", default="data/bars")
    ap.add_argument("--out", default=str(DEFAULT_REPORT_DIR))
    ap.add_argument("--evidence-out", default=str(DEFAULT_EVIDENCE_PATH))
    ap.add_argument(
        "--altdata",
        choices=sorted(ALTDATA_FEATURES),
        help="also backtest with this alt-data feature (backfills EDGAR/news; slow)",
    )
    args = ap.parse_args(argv)

    if env is None:
        env = {**load_env_file(Path(".env")), **os.environ}
    try:
        settings = Settings.from_env(env)
        client = make_data_client(settings)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    universe_name = args.universe or settings.universe_name
    members = universe_for(universe_name)
    universe = args.symbols.split(",") if args.symbols else sorted(members)
    if BENCHMARK not in universe:
        universe = [BENCHMARK, *universe]
    try:
        first_bar = fetch_start(args.start, args.history_start)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    loaded = load_daily_bars(
        client, universe, first_bar, args.end, BarCache(args.cache), feed=args.feed
    )
    if BENCHMARK not in loaded.bars:
        print(f"error: no {BENCHMARK} bars: {loaded.skipped.get(BENCHMARK)}", file=sys.stderr)
        return 2

    try:
        name = args.strategy or settings.strategy_name
        strat = get_strategy(name, strategy_config(name, settings))
        strat = strat.with_config(apply_overrides(strat.config, args.set))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    risk = settings.risk
    try:
        bt = BacktestConfig(
            args.start,
            args.end,
            args.equity,
            gated=not args.no_gate,
            resume_after_days=args.resume_after_days,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sectors = {s: members.get(s, "") for s in loaded.bars}
    result = run_backtest(loaded.bars, sectors, strat, risk, bt)
    stability = stability_check(loaded.bars, sectors, strat, risk, bt) if args.stability else None
    notes = [f"skipped {s}: {why}" for s, why in sorted(loaded.skipped.items())]
    notes.append(
        f"{len(loaded.bars)} symbols ({universe_name}), {args.feed.upper()} feed, "
        f"history from {first_bar}"
    )
    run_id = run_id_for(strat, risk, bt, datetime.now(ET).date())
    if universe_name != "sp100":
        run_id += f"-{universe_name}"
    if args.altdata:
        notes += altdata_comparison(args.altdata, loaded.bars, sectors, settings, bt, result, strat)
    path = write_report(
        render_report(result, strat, risk, bt, run_id, notes, stability, args.stability_advisory),
        run_id,
        args.out,
    )
    final = verdict(result, stability, advisory=args.stability_advisory)
    save_evidence(Path(args.evidence_out), evidence_for(result, bt, run_id, final, universe_name))
    print(f"{final}: {path} (evidence: {args.evidence_out})")
    return 0 if final == "ACCEPTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
