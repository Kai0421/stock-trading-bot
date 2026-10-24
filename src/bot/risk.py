# Author: Kai0421
"""Risk controls: success-probability gate, tradable cash, sizing and buy approval.

Pure, deterministic functions (docs/01-trading-fundamentals.md, docs/03-strategy.md).
Everything fails closed: missing or inconsistent data means no buy.

- ``probability_gate``: n >= 100, win rate >= target, 95% Wilson lower bound >= floor,
  positive expectancy. With ``gate_mode="expectancy"``: n >= 100 and a positive 95% lower
  bound of the mean R instead of the win-rate tests (sizing then risks at most 0.5%).
- ``tradable_cash``: ``min(cash, non_marginable_buying_power) - unsettled proceeds -
  open buy orders - reserve``. Alpaca ``buying_power`` is never read.
- ``approve_buys``: processes a batch one order at a time, subtracting each order's cost
  (and counting its position and sector) before checking the next.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from bot.config import HARD_MIN_WIN_PROB_FLOOR, MIN_SIGNATURE_TRADES, RiskConfig

WILSON_Z_95 = 1.959963984540054
SIGNATURE_DISABLE_WINDOW = 30
# Expectancy mode: a signature is disabled when its last 30 live wins fall more than this
# below its backtest win rate (the fixed floor would switch off every low-win strategy).
EXPECTANCY_DISABLE_MARGIN = 0.15
ALLOWED_ASSET_CLASSES = frozenset({"us_equity"})
# Leveraged / inverse / volatility products: rejected even if they reach the whitelist.
LEVERAGED_OR_INVERSE = frozenset(
    {
        "TQQQ", "SQQQ", "QLD", "QID", "SSO", "SDS", "UPRO", "SPXU", "SPXL", "SPXS", "SH",
        "PSQ", "DOG", "DXD", "SDOW", "UDOW", "TNA", "TZA", "SOXL", "SOXS", "TECL", "TECS",
        "FAS", "FAZ", "LABU", "LABD", "TMF", "TMV", "TBT", "UVXY", "SVXY", "VXX", "VIXY",
        "UVIX", "SVIX", "NUGT", "DUST", "JNUG", "JDST", "ERX", "ERY", "YINN", "YANG",
        "FNGU", "FNGD", "BULZ", "BERZ", "TSLL", "NVDL", "NVDU", "TSLQ", "NVDQ",
    }
)  # fmt: skip


# --------------------------------------------------------- probability gate


def wilson_lower_bound(wins: int, n: int, z: float = WILSON_Z_95) -> float:
    """Lower bound of the Wilson score interval for a binomial proportion."""
    if n <= 0 or not 0 <= wins <= n:
        raise ValueError(f"need 0 <= wins <= n and n > 0 (got wins={wins}, n={n})")
    p = wins / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (centre - margin) / (1 + z2 / n)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    win_rate: float
    lower_bound: float


def mean_r_lower_bound(mean_r: float, sd_r: float, n: int, z: float = WILSON_Z_95) -> float:
    """Normal-approximation 95% lower bound of the mean R multiple (NaN if unknown)."""
    if n <= 1 or not math.isfinite(mean_r) or not math.isfinite(sd_r):
        return math.nan
    return mean_r - z * sd_r / math.sqrt(n)


def probability_gate(
    wins: int, n: int, expectancy_r: float, cfg: RiskConfig, sd_r: float = math.nan
) -> GateResult:
    """Success-probability gate on out-of-sample trades with the same setup signature."""
    min_n = max(cfg.min_signature_trades, MIN_SIGNATURE_TRADES)
    if n < min_n:
        return GateResult(False, f"only {n} matching trades (need {min_n})", math.nan, math.nan)
    win_rate = wins / n
    lb = wilson_lower_bound(wins, n)
    if cfg.gate_mode == "expectancy":
        if sd_r is None or not math.isfinite(sd_r):
            return GateResult(False, "no R spread in the evidence; re-run backtest", win_rate, lb)
        r_lb = mean_r_lower_bound(expectancy_r, sd_r, n)
        if not r_lb > 0:
            reason = f"95% lower bound of mean R {r_lb:+.3f}R is not positive"
            return GateResult(False, reason, win_rate, lb)
        return GateResult(True, f"passed (mean R lower bound {r_lb:+.3f}R)", win_rate, lb)
    floor = max(cfg.min_win_prob_floor, HARD_MIN_WIN_PROB_FLOOR)
    if win_rate < cfg.min_win_prob_target:
        reason = f"win rate {win_rate:.1%} below target {cfg.min_win_prob_target:.0%}"
        return GateResult(False, reason, win_rate, lb)
    if lb < floor:
        reason = f"95% lower bound {lb:.1%} below floor {floor:.0%}"
        return GateResult(False, reason, win_rate, lb)
    if not expectancy_r > 0:
        return GateResult(False, f"expectancy {expectancy_r:.3f}R is not positive", win_rate, lb)
    return GateResult(True, "passed", win_rate, lb)


def signature_disabled(
    outcomes: Sequence[bool], cfg: RiskConfig, backtest_win_rate: float | None = None
) -> bool:
    """True if the last 30 live/paper outcomes of a signature win below the floor.

    In expectancy mode the floor is the signature's backtest win rate minus 15 points;
    without a backtest win rate the fixed floor applies (fails closed)."""
    if len(outcomes) < SIGNATURE_DISABLE_WINDOW:
        return False
    recent = outcomes[-SIGNATURE_DISABLE_WINDOW:]
    floor = max(cfg.min_win_prob_floor, HARD_MIN_WIN_PROB_FLOOR)
    if cfg.gate_mode == "expectancy" and backtest_win_rate is not None:
        floor = max(0.0, backtest_win_rate - EXPECTANCY_DISABLE_MARGIN)
    return sum(recent) / SIGNATURE_DISABLE_WINDOW < floor


# --------------------------------------------------------- cash and sizing


@dataclass(frozen=True)
class Account:
    """Fresh account snapshot. ``buying_power`` is kept only to show it is never used."""

    cash: float
    non_marginable_buying_power: float
    equity: float
    buying_power: float = 0.0


def tradable_cash(
    account: Account, unsettled_proceeds: float, open_buy_orders_cost: float, cfg: RiskConfig
) -> float:
    """Money that may be spent on new buys. Can be negative (then nothing is bought)."""
    if unsettled_proceeds < 0 or open_buy_orders_cost < 0:
        raise ValueError("unsettled proceeds and open buy orders cost must be >= 0")
    reserve = cfg.cash_reserve_pct * max(account.equity, 0.0)
    base = min(account.cash, account.non_marginable_buying_power)
    return base - unsettled_proceeds - open_buy_orders_cost - reserve


def size_position(
    equity: float,
    limit_price: float,
    stop_price: float,
    cash_available: float,
    cfg: RiskConfig,
    est_fees: float = 0.0,
) -> int:
    """Whole shares, rounded down: min(risk size, cash cap, max position size)."""
    if not limit_price > 0 or not 0 < stop_price < limit_price:
        raise ValueError(f"need 0 < stop < limit (got stop={stop_price}, limit={limit_price})")
    if equity <= 0 or cash_available - est_fees <= 0:
        return 0
    r = limit_price - stop_price
    by_risk = math.floor(equity * cfg.effective_risk_per_trade / r)
    by_cash = math.floor((cash_available - est_fees) / limit_price)
    by_size = math.floor(equity * cfg.max_position_pct / limit_price)
    return max(0, min(by_risk, by_cash, by_size))


def validate_sell(qty: int, held: int, open_sell_qty: int) -> int:
    """Return ``qty`` if it can be sold without going short; raise otherwise."""
    if qty <= 0:
        raise ValueError(f"sell quantity must be positive (got {qty})")
    available = held - open_sell_qty
    if qty > available:
        raise ValueError(f"sell {qty} exceeds available {available} (held {held})")
    return qty


def check_instrument(symbol: str, asset_class: str, whitelist: frozenset[str]) -> str | None:
    """None if the instrument may be bought, else the reason it is rejected."""
    if asset_class not in ALLOWED_ASSET_CLASSES:
        return f"{symbol}: asset class {asset_class!r} not allowed"
    if symbol in LEVERAGED_OR_INVERSE:
        return f"{symbol}: leveraged/inverse/volatility product"
    if symbol not in whitelist:
        return f"{symbol}: not in the whitelist"
    return None


# --------------------------------------------------------- kill switches


@dataclass(frozen=True)
class KillState:
    halt_new_orders_today: bool = False
    paused: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def blocks_buys(self) -> bool:
        return self.halt_new_orders_today or self.paused


def kill_switch(
    equity: float, start_of_day_equity: float, peak_equity: float, cfg: RiskConfig
) -> KillState:
    """Daily-loss halt and drawdown pause. Invalid inputs pause the bot."""
    if equity <= 0 or start_of_day_equity <= 0 or peak_equity <= 0:
        return KillState(paused=True, reasons=("invalid equity figures",))
    reasons: list[str] = []
    daily = equity / start_of_day_equity - 1
    drawdown = equity / peak_equity - 1
    # Compare against threshold equity (with a cent of tolerance, erring towards tripping):
    # ratios like 90_000 / 100_000 - 1 are not exactly -0.10 in floating point.
    halt = equity <= start_of_day_equity * (1 - cfg.daily_loss_kill_pct) + 0.01
    paused = equity <= peak_equity * (1 - cfg.drawdown_kill_pct) + 0.01
    if halt:
        reasons.append(f"daily loss {daily:.2%}")
    if paused:
        reasons.append(f"drawdown {drawdown:.2%} from peak")
    return KillState(halt_new_orders_today=halt, paused=paused, reasons=tuple(reasons))


# --------------------------------------------------------- buy approval


@dataclass(frozen=True)
class BuyRequest:
    symbol: str
    limit_price: float
    stop_price: float
    sector: str
    asset_class: str = "us_equity"
    est_fees: float = 0.0


@dataclass(frozen=True)
class PortfolioState:
    """Inputs for approval. ``account`` is None when the fresh fetch failed."""

    account: Account | None
    margin_lock_verified: bool
    unsettled_proceeds: float
    open_buy_orders_cost: float
    open_position_sectors: tuple[str, ...]  # one entry per open position or pending buy
    kill: KillState
    whitelist: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class BuyDecision:
    symbol: str
    shares: int
    cost: float
    reason: str
    critical: bool = False

    @property
    def approved(self) -> bool:
        return self.shares > 0


def _reject(req: BuyRequest, reason: str, critical: bool = False) -> BuyDecision:
    return BuyDecision(req.symbol, 0, 0.0, reason, critical)


def _global_block(state: PortfolioState) -> tuple[str, bool] | None:
    acct = state.account
    if acct is None:
        return "account fetch failed: no orders", False
    if acct.cash < 0:
        return "negative cash (margin used): buys blocked, manual review", True
    if not state.margin_lock_verified:
        return "margin/shorting lock not verified: buys blocked", True
    if state.kill.blocks_buys:
        return "kill switch: " + ", ".join(state.kill.reasons or ("active",)), False
    return None


def approve_buys(
    requests: Sequence[BuyRequest], state: PortfolioState, cfg: RiskConfig
) -> list[BuyDecision]:
    """Approve and size a batch of buys, one at a time, never exceeding tradable cash."""
    block = _global_block(state)
    if block is not None:
        return [_reject(r, *block) for r in requests]
    assert state.account is not None
    equity = state.account.equity
    cash = tradable_cash(state.account, state.unsettled_proceeds, state.open_buy_orders_cost, cfg)
    positions = len(state.open_position_sectors)
    sectors = Counter(state.open_position_sectors)
    seen: set[str] = set()

    out: list[BuyDecision] = []
    for req in requests:
        reason = check_instrument(req.symbol, req.asset_class, state.whitelist)
        if reason is not None:
            out.append(_reject(req, reason))
            continue
        if req.symbol in seen:
            out.append(_reject(req, f"{req.symbol}: duplicate in batch"))
            continue
        if not req.sector:
            out.append(_reject(req, f"{req.symbol}: unknown sector"))
            continue
        if not 0 < req.stop_price < req.limit_price:
            out.append(_reject(req, f"{req.symbol}: invalid stop {req.stop_price}"))
            continue
        if positions >= cfg.max_open_positions:
            out.append(_reject(req, f"max open positions ({cfg.max_open_positions}) reached"))
            continue
        if sectors[req.sector] >= cfg.max_per_sector:
            out.append(_reject(req, f"max per sector reached for {req.sector}"))
            continue
        shares = size_position(equity, req.limit_price, req.stop_price, cash, cfg, req.est_fees)
        if shares <= 0:
            out.append(_reject(req, "not enough tradable cash"))
            continue
        cost = shares * req.limit_price + req.est_fees
        cash -= cost
        positions += 1
        sectors[req.sector] += 1
        seen.add(req.symbol)
        out.append(BuyDecision(req.symbol, shares, cost, "approved"))
    return out
