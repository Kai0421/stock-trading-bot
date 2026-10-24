# Author: Kai0421
"""Decision layer: pure functions from snapshots to trade decisions (docs/19-layers.md).

Nothing in here talks to the broker, the data APIs, the journal, the clock or Slack.
Every function takes plain values gathered by ``bot.gather`` (bars, positions, open
orders, the journal's book, evidence) and returns plain values (plans, signal records,
order intents, stop changes) that ``bot.execute`` turns into broker orders and journal
rows. The pluggable strategies (``bot.strategies``, ``bot.intraday.strategies``) and the
risk rules (``bot.risk``) are called from here and only from here.

Layer rules: ``decide`` may import strategies, ``risk``, ``evidence`` and the plain data
classes; it must never import ``bot.broker.Broker``, ``bot.journal.Journal``,
``bot.data`` or ``bot.notify``. ``tests/test_layers.py`` enforces this.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from bot.broker import AccountSnapshot, BrokerOrder, BrokerPosition
from bot.config import INTRADAY_MIN_EQUITY, RiskConfig
from bot.evidence import Evidence, EvidenceGate
from bot.intraday.base import IntradayPosition, IntradayStrategy, minute_of_day
from bot.journal import PlanRow, PositionRow
from bot.risk import (
    BuyDecision,
    BuyRequest,
    KillState,
    PortfolioState,
    approve_buys,
    check_instrument,
    kill_switch,
)
from bot.sentiment import DEFAULT_THRESHOLDS, Snapshot, classify
from bot.strategies import PositionState, Strategy
from bot.universe import BENCHMARK

ENTRY_CUTOFF = time(11, 0)  # unfilled entries are cancelled at 11:00 ET (docs/03)
UNKNOWN_STOP_PCT = 0.05  # protective stop for a position the bot did not open
STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}
DONE_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}
ALTDATA_VETOES = ("news_shock_veto", "insider_heavy_selling_30d")

# Intraday (docs/17-intraday.md)
STALE_ENTRY_MINUTES = 10  # an unfilled intraday entry is cancelled after two bars
FRESH_BAR_MINUTES = 10  # act only on a bar that closed within the last 10 minutes
INTRADAY_ENTRY_SUFFIX = "-ientry"

# Overnight (docs/19-overnight-hold.md): buy in the closing auction, sell in the opening one
OVERNIGHT_NAME = "overnight_hold"
OVERNIGHT_ENTRY_SUFFIX = "-entry-close"
OVERNIGHT_SELL_SUFFIX = "-exit-open"
OVERNIGHT_LATE_SUFFIX = "-exit-late"
OVERNIGHT_LIMIT_PCT = 0.01  # late-day limit buy at most 1% above the last price
OVERNIGHT_STOP_PCT = 0.05  # broker-side stop 5% under the limit, placed once filled
OVERNIGHT_FILTERS = frozenset({"all", "notfri"})  # filters that need no same-day close


# ================================================================= outputs


@dataclass(frozen=True)
class Alert:
    """An alert the decision implies; the engine sends it (level is a ``Level`` name)."""

    level: str
    title: str
    text: str = ""


@dataclass(frozen=True)
class SignalRecord:
    """One row of the journal's ``signals`` table: a setup and what was decided."""

    day: date
    symbol: str
    signature: str
    win_prob: float | None
    lower_bound: float | None
    taken: bool
    reason: str
    features: dict[str, Any]


@dataclass(frozen=True)
class EntryPlan:
    """Premarket's output: ranked plans plus every signal in the order it was decided."""

    plans: list[PlanRow]
    signals: list[SignalRecord]


@dataclass(frozen=True)
class ProtectAction:
    """Cover ``qty`` unprotected shares: a stop at ``stop`` or, if price is already
    below it, a market sell."""

    symbol: str
    qty: int
    stop: float
    cid: str
    sell: bool
    current_price: float
    row: PositionRow | None
    expected: bool = False  # tonight's overnight fill: its stop goes in after the close


@dataclass(frozen=True)
class ExitIntent:
    """Sell a position (``full``) or part of it, decided at yesterday's close."""

    row: PositionRow
    kind: str
    qty: int  # shares to sell
    held: int  # shares held at the broker
    full: bool
    cid: str


@dataclass(frozen=True)
class StopChange:
    """Replace a working stop: raise its price or trim its quantity to the shares held."""

    order_id: str
    symbol: str
    stop_price: float | None = None
    qty: int | None = None
    old_price: float | None = field(default=None, compare=False)  # Slack alert only
    held: int | None = field(default=None, compare=False)  # Slack alert only


@dataclass(frozen=True)
class EntryOrder:
    """A sized OTO entry: limit buy plus its broker-side stop (rule 6)."""

    symbol: str
    shares: int
    limit: float
    stop: float
    cid: str
    sector: str
    win_prob: float
    cost: float


# ================================================================= shared helpers


def flatten_orders(orders: Sequence[BrokerOrder]) -> list[BrokerOrder]:
    """Orders and all their legs (OTO stop legs live inside the entry)."""
    out: list[BrokerOrder] = []
    for o in orders:
        out.append(o)
        out.extend(flatten_orders(o.legs))
    return out


def open_stops(orders: Sequence[BrokerOrder]) -> dict[str, list[BrokerOrder]]:
    """Working sell stops per symbol (``held`` legs belong to unfilled entries)."""
    out: dict[str, list[BrokerOrder]] = defaultdict(list)
    for o in flatten_orders(orders):
        if o.side == "sell" and o.type in STOP_TYPES and o.status not in DONE_STATUSES | {"held"}:
            out[o.symbol].append(o)
    return out


def stops_by_id(orders: Sequence[BrokerOrder]) -> dict[str, BrokerOrder]:
    return {o.id: o for o in flatten_orders(orders) if o.type in STOP_TYPES}


def held_symbols(
    positions: Iterable[str], rows: Iterable[str], open_orders: Sequence[BrokerOrder]
) -> set[str]:
    """Symbols already held at the broker, in the journal or with a working buy."""
    return set(positions) | set(rows) | {o.symbol for o in open_orders if o.side == "buy"}


def buy_block_reason(
    paused: bool, halt_today: bool, halt_day: str | None, today: date
) -> str | None:
    """Kill-switch flags that block every new buy today."""
    if paused:
        return "paused (drawdown kill switch or manual)"
    if halt_today and halt_day == today.isoformat():
        return "daily-loss kill switch: no new orders today"
    return None


def evidence_problem(
    evidence: Evidence | None, strategy: str, path: Any, universe: str = "sp100"
) -> tuple[str, Alert] | None:
    """Why the strategy's backtest evidence can't be used (note, alert), or None."""
    if evidence is None:
        return "no backtest evidence", Alert(
            "WARN",
            "No trades: backtest results missing",
            f"{path} is missing. Run the backtest first.",
        )
    if evidence.strategy != strategy:
        return f"evidence is for {evidence.strategy}, not {strategy}", Alert(
            "WARN",
            "No trades: backtest is for a different strategy",
            f"{path} tested {evidence.strategy}, but the bot is set to trade "
            f"{strategy}. Run the backtest with --strategy {strategy}.",
        )
    if evidence.universe != universe:
        return f"evidence is for universe {evidence.universe}, not {universe}", Alert(
            "WARN",
            "No trades: backtest is for a different stock list",
            f"{path} was tested on {evidence.universe}, but the bot is set to "
            f"UNIVERSE={universe}. Run the backtest with --universe {universe}.",
        )
    return None


def _num(x: float) -> float | None:
    return None if pd.isna(x) else x


# ================================================================= protect / fills


def protect_actions(
    positions: Mapping[str, BrokerPosition],
    open_orders: Sequence[BrokerOrder],
    rows: Mapping[str, PositionRow],
    now: datetime,
) -> list[ProtectAction]:
    """Every position must have broker-side stops covering all its shares (rule 6)."""
    stops = open_stops(open_orders)
    # An overnight-mode sell at the open replaces the stop (Alpaca won't hold both for
    # the same shares); it counts as cover until the open (decide.overnight_sells).
    opening = defaultdict(int)
    for o in flatten_orders(open_orders):
        if o.side == "sell" and o.client_order_id.endswith(OVERNIGHT_SELL_SUFFIX):
            opening[o.symbol] += int(o.qty - o.filled_qty)
    out = []
    for symbol, bp in positions.items():
        covered = sum(int(o.qty) for o in stops.get(symbol, [])) + opening[symbol]
        if covered >= bp.qty:
            continue
        row = rows.get(symbol)
        stop = row.stop if row else round(bp.avg_entry_price * (1 - UNKNOWN_STOP_PCT), 2)
        sell = bool(bp.current_price and stop >= bp.current_price)
        cid = f"{now.date()}-{symbol}-protect-{now:%H%M}"
        expected = bool(row and row.signature == OVERNIGHT_NAME and row.entry_date == now.date())
        out.append(
            ProtectAction(
                symbol, bp.qty - covered, stop, cid, sell, bp.current_price, row, expected
            )
        )
    return out


def adopted_stop(
    stops: Sequence[BrokerOrder], entry_stop: float | None, avg_entry_price: float
) -> float:
    """Initial stop for a newly filled position: its working stop, else the entry
    order's stop, else 5% below the fill. Always strictly below the fill."""
    if stops:
        initial = stops[0].stop_price or 0.0
    elif entry_stop:
        initial = float(entry_stop)
    else:
        initial = round(avg_entry_price * (1 - UNKNOWN_STOP_PCT), 2)
    if not 0 < initial < avg_entry_price:
        initial = round(avg_entry_price * (1 - UNKNOWN_STOP_PCT), 2)
    return initial


def exit_reason(order: BrokerOrder | None, row: PositionRow) -> str:
    """Why a position closed, read from the last sell's client order id or type."""
    if order is None:
        return "unknown"
    cid = order.client_order_id
    if "-exit-" in cid:
        return cid.rsplit("-exit-", 1)[1]
    if cid.endswith("-partial"):
        return "partial_profit"
    if order.type in STOP_TYPES:
        return "trail" if (order.stop_price or 0) >= row.entry_price else "stop"
    return "manual"


# ================================================================= swing: premarket


def premarket_blocker(
    paused: bool,
    spy: pd.DataFrame | None,
    prev: date,
    evidence: Evidence | None,
    evidence_path: Any,
    strategy: str,
    early_close: bool,
    universe: str = "sp100",
) -> tuple[str, Alert | None] | None:
    """Why premarket plans nothing today (note for the heartbeat, optional alert)."""
    if paused:
        return "bot is paused (drawdown kill switch or manual); resume to trade", None
    if spy is None or spy.empty or spy.index[-1].date() != prev:
        last = None if spy is None or spy.empty else spy.index[-1].date()
        alert = Alert(
            "WARN",
            "Market data is out of date",
            f"The latest {BENCHMARK} price is from {last}, but {prev} is needed. No trades today.",
        )
        return f"stale market data ({BENCHMARK} last bar {last})", alert
    problem = evidence_problem(evidence, strategy, evidence_path, universe)
    if problem is not None:
        return problem
    if early_close:
        return "early close today: no new entries", None
    return None


def setups_on(tables: Mapping[str, pd.DataFrame], prev: date) -> list[str]:
    """Symbols whose entry table has a setup on the previous session's bar."""
    return [
        s
        for s, t in tables.items()
        if not t.empty and t.index[-1].date() == prev and t["setup"].iloc[-1]
    ]


def altdata_wanted(setups: Sequence[str], held: set[str], earnings_policy: str) -> list[str]:
    """Setups that will reach the alt-data veto (not held, earnings policy not strict)."""
    if earnings_policy == "strict":
        return []
    return [s for s in setups if s not in held]


def plan_entries(
    *,
    today: date,
    prev: date,
    strategy: Strategy,
    features: Mapping[str, pd.DataFrame],
    tables: Mapping[str, pd.DataFrame],
    held: set[str],
    open_slots: int,
    gate: EvidenceGate,
    risk: RiskConfig,
    sectors: Mapping[str, str],
    earnings_policy: str,
    paper_trial: bool,
    altdata: Mapping[str, dict[str, bool]] | None,
    altdata_features: frozenset[str],
    open_sectors: Sequence[str] = (),
) -> EntryPlan:
    """Gate every setup, rank survivors by win rate then strategy score, fill free slots.

    ``open_sectors`` has one entry per open position or pending buy; a setup whose sector
    is already full (``risk.max_per_sector``) is not planned, so the plan matches what the
    risk check will allow at the open.

    ``altdata`` holds the alt-data features for each setup in ``altdata_wanted`` (None
    when no provider is configured)."""
    signals: list[SignalRecord] = []
    candidates: list[tuple[float, float, str, PlanRow, dict[str, Any]]] = []
    for symbol in setups_on(tables, prev):
        row = tables[symbol].iloc[-1]
        f = features[symbol].iloc[-1]
        signature = row["signature"]
        feats: dict[str, Any] = {
            "strategy": strategy.name,
            "close": round(float(f["close"]), 2),
            "atr": round(float(f["atr"]), 4),
            "score": round(float(row["score"]), 4),
            "flags": [],
        }
        if paper_trial:
            feats["paper_trial"] = True
        if "rsi_min" in row.index:  # Trend Pullback's, kept for the reviews
            feats["rsi_min"] = round(float(row["rsi_min"]), 2)
        if "momentum" in f.index:
            feats["momentum"] = round(float(f["momentum"]), 4)

        def reject(reason: str, wp=None, lb=None, sym=symbol, sig=signature, ft=feats) -> None:
            ft = {**ft, "flags": list(ft["flags"])}  # as it stood when rejected
            signals.append(SignalRecord(today, sym, sig, wp, lb, False, reason, ft))

        if symbol in held:
            reject("already held or ordered")
            continue
        if earnings_policy == "strict":
            reject("earnings date unknown (policy=strict)")
            continue
        feats["flags"].append("earnings date unknown (policy=flag)")
        if altdata is not None and altdata_features:
            alt = altdata[symbol]
            feats["altdata"] = alt
            vetoes = [k for k in ALTDATA_VETOES if k in altdata_features and alt.get(k)]
            if vetoes:
                reject(f"alt-data veto: {', '.join(vetoes)}")
                continue
        g = gate.gate(signature, pd.Timestamp(today), risk)
        if not g.passed:
            reject(g.reason, _num(g.win_rate), _num(g.lower_bound))
            continue
        plan = PlanRow(
            day=today,
            rank=0,
            symbol=symbol,
            signature=signature,
            win_prob=g.win_rate,
            atr=float(f["atr"]),
            momentum=float(row["score"]),  # the strategy's ranking score
            sector=sectors.get(symbol, ""),
            last_close=float(f["close"]),
        )
        feats["lower_bound"] = round(g.lower_bound, 4)
        candidates.append((-g.win_rate, -plan.momentum, symbol, plan, feats))

    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    plans: list[PlanRow] = []
    sector_count = Counter(open_sectors)
    for _, _, symbol, plan, feats in candidates:
        full = bool(plan.sector) and sector_count[plan.sector] >= risk.max_per_sector
        taken = len(plans) < open_slots and not full
        if taken:
            plans.append(replace(plan, rank=len(plans) + 1))
            sector_count[plan.sector] += 1
        reason = (
            "planned" if taken
            else f"max per sector reached for {plan.sector}" if full
            else "no slots"
        )  # fmt: skip
        signals.append(
            SignalRecord(today, symbol, plan.signature, plan.win_prob, feats.get("lower_bound"),
                         taken, reason, feats)
        )  # fmt: skip
    return EntryPlan(plans, signals)


def free_slots(
    risk: RiskConfig, positions: Iterable[str], open_orders: Sequence[BrokerOrder]
) -> int:
    open_buys = {o.symbol for o in open_orders if o.side == "buy"}
    return max(risk.max_open_positions - len(set(positions) | open_buys), 0)


# ================================================================= swing: entries / exits


def entry_prices(strategy: Strategy | IntradayStrategy, price: float, stop_input: Any):
    """(limit, stop) for an entry at the latest ``price``; ValueError if no valid stop.

    ``stop_input`` is the ATR for a swing strategy, the setup bar for an intraday one."""
    limit = strategy.limit_price(price)
    return limit, strategy.stop_for(limit, stop_input)


def size_entry(
    *,
    symbol: str,
    limit: float,
    stop: float,
    sector: str,
    cid: str,
    win_prob: float,
    snap: AccountSnapshot,
    open_orders: Sequence[BrokerOrder],
    positions: Iterable[str],
    sector_of: Mapping[str, str],
    unsettled: float,
    committed_cost: Sequence[float],
    committed_sectors: Sequence[str],
    risk: RiskConfig,
) -> tuple[EntryOrder | None, BuyDecision]:
    """Size one entry against a fresh account snapshot with ``approve_buys``.

    ``committed_*`` are dry-run orders from this job, which never appear as open orders."""
    open_buys = [o for o in open_orders if o.side == "buy"]
    open_cost = sum((o.qty - o.filled_qty) * (o.limit_price or 0) for o in open_buys)
    sectors = (
        tuple(sector_of.get(s, "") for s in positions)
        + tuple(sector_of.get(o.symbol, "") for o in open_buys)
        + tuple(committed_sectors)
    )
    state = PortfolioState(
        account=snap.account,
        margin_lock_verified=True,
        unsettled_proceeds=unsettled,
        open_buy_orders_cost=open_cost + sum(committed_cost),
        open_position_sectors=sectors,
        kill=KillState(),
        whitelist=frozenset(sector_of),
    )
    (decision,) = approve_buys([BuyRequest(symbol, limit, stop, sector)], state, risk)
    if not decision.approved:
        return None, decision
    order = EntryOrder(symbol, decision.shares, limit, stop, cid, sector, win_prob, decision.cost)
    return order, decision


def instrument_problem(symbol: str, whitelist: frozenset[str]) -> str | None:
    return check_instrument(symbol, "us_equity", whitelist)


def exit_intents(
    rows: Mapping[str, PositionRow], positions: Mapping[str, BrokerPosition], today: date
) -> list[ExitIntent]:
    """Pending exits (decided at the previous close) for positions still held."""
    out = []
    for row in rows.values():
        if row.pending_exit is None:
            continue
        kind, qty = row.pending_exit
        bp = positions.get(row.symbol)
        if bp is None:
            continue
        full = kind != "partial_profit" or qty >= bp.qty
        cid = f"{today}-{row.symbol}-" + (f"exit-{kind}" if full else "partial")
        out.append(ExitIntent(row, kind, bp.qty if full else qty, bp.qty, full, cid))
    return out


def sentiment_exits(
    rows: Mapping[str, PositionRow],
    positions: Mapping[str, BrokerPosition],
    snapshots: Mapping[str, Snapshot],
    today: date,
) -> list[ExitIntent]:
    """Full exits for held stocks whose fresh Stocktwits snapshot is a ``sell_warning``.

    Held-ness comes from the bot's own book, never from the collector's flag. A position that
    already has a pending exit is left to it. Selling only reduces risk.
    """
    out = []
    for row in rows.values():
        snap, bp = snapshots.get(row.symbol), positions.get(row.symbol)
        if snap is None or bp is None or row.pending_exit is not None:
            continue
        if classify(replace(snap, held=True), DEFAULT_THRESHOLDS) != "sell_warning":
            continue
        cid = f"{today}-{row.symbol}-exit-sentiment"
        out.append(ExitIntent(row, "sentiment", bp.qty, bp.qty, True, cid))
    return out


def entries_to_cancel(open_orders: Sequence[BrokerOrder], now: datetime) -> list[BrokerOrder]:
    """Swing entries still unfilled at the 11:00 cutoff."""
    if now.time() < ENTRY_CUTOFF:
        return []
    return [o for o in open_orders if o.side == "buy" and o.client_order_id.endswith("-entry")]


def stop_changes(
    rows: Mapping[str, PositionRow],
    open_orders: Sequence[BrokerOrder],
    positions: Mapping[str, BrokerPosition],
) -> list[StopChange]:
    """Raise broker stops to the journal's stop (never down); trim them to shares held."""
    stops = stops_by_id(open_orders)
    out = []
    for row in rows.values():
        order = stops.get(row.stop_order_id or "")
        bp = positions.get(row.symbol)
        if order is None or bp is None:
            continue
        if row.stop > (order.stop_price or 0) + 0.005:
            out.append(
                StopChange(
                    order.id,
                    row.symbol,
                    stop_price=row.stop,
                    old_price=order.stop_price,
                    held=int(bp.qty),
                )
            )
        elif order.qty > bp.qty:
            out.append(StopChange(order.id, row.symbol, qty=bp.qty, held=int(bp.qty)))
    return out


# ================================================================= swing: eod


@dataclass(frozen=True)
class EodDecision:
    """Kill-switch outcome and per-position updates from today's close."""

    kill: KillState
    rows: list[PositionRow] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def eod_kill(equity: float, prev: float | None, peak: float, risk: RiskConfig) -> KillState:
    return kill_switch(equity, prev or equity, peak, risk)


def manage_positions(
    rows: Mapping[str, PositionRow],
    bars: Mapping[str, pd.DataFrame],
    strategy: Strategy,
    today: date,
    days_held: Mapping[str, int],
) -> tuple[list[PositionRow], list[str]]:
    """The strategy's exit and stop decisions from today's close, for tomorrow."""
    updated, anomalies = [], []
    for symbol, row in rows.items():
        df = bars.get(symbol)
        if df is None or df.empty or df.index[-1].date() != today:
            anomalies.append(f"{symbol}: no bar for {today}; stop/exit not updated")
            continue
        f = strategy.features(df).iloc[-1]
        held = days_held[symbol]
        pos = PositionState(
            symbol, row.entry_price, row.initial_stop, row.stop, row.shares, held,
            row.partial_taken,
        )  # fmt: skip
        actions, new_stop = strategy.manage(pos, f.to_dict())
        pending = (actions[0].kind, actions[0].shares) if actions else None
        updated.append(replace(row, stop=new_stop, days_held=held, pending_exit=pending))
    return updated, anomalies


# ================================================================= intraday


def intraday_features(
    strategy: IntradayStrategy,
    bars: Mapping[str, pd.DataFrame],
    daily: Mapping[str, pd.DataFrame],
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """The strategy's features on today's completed bars; warnings for bad symbols."""
    spy = daily.get(BENCHMARK)
    feats: dict[str, pd.DataFrame] = {}
    warnings = []
    for s, df in bars.items():
        try:
            feats[s] = strategy.prepare(df, daily.get(s), spy)
        except ValueError as exc:
            warnings.append(f"intraday features {s}: {exc}")
    return feats, warnings


def stale_intraday_entries(open_orders: Sequence[BrokerOrder], now: datetime) -> list[BrokerOrder]:
    out = []
    for o in open_orders:
        if o.side != "buy" or not o.client_order_id.endswith(INTRADAY_ENTRY_SUFFIX):
            continue
        if now - (o.submitted_at or now) >= timedelta(minutes=STALE_ENTRY_MINUTES):
            out.append(o)
    return out


@dataclass(frozen=True)
class IntradayExit:
    row: PositionRow
    qty: int
    cid: str


def intraday_manage(
    rows: Mapping[str, PositionRow],
    positions: Mapping[str, BrokerPosition],
    open_orders: Sequence[BrokerOrder],
    feats: Mapping[str, pd.DataFrame],
    strategy: IntradayStrategy,
    entry_bars: Mapping[str, str | None],
    today: date,
) -> list[IntradayExit | tuple[PositionRow, float, str | None]]:
    """Per position opened today: an exit, or a raised stop ``(row, new_stop,
    stop_order_id or None)``, in the journal's order."""
    stops = stops_by_id(open_orders)
    out: list[IntradayExit | tuple[PositionRow, float, str | None]] = []
    for symbol, row in rows.items():
        f = feats.get(symbol)
        bp = positions.get(symbol)
        if f is None or f.empty or bp is None:
            continue
        placed = entry_bars.get(symbol)
        since = pd.Timestamp(placed) if placed else f.index[0]
        held = int((f.index >= since.floor(f"{strategy.bar_minutes}min")).sum())
        pos = IntradayPosition(symbol, row.entry_price, row.initial_stop, row.stop, bp.qty, held)
        reason, new_stop = strategy.manage(pos, f.iloc[-1].to_dict())
        if reason is not None:
            out.append(IntradayExit(row, bp.qty, f"{today}-{symbol}-exit-{reason}"))
        elif new_stop > row.stop + 0.005:
            order = stops.get(row.stop_order_id or "")
            out.append((row, new_stop, order.id if order is not None else None))
    return out


def intraday_setups(
    feats: Mapping[str, pd.DataFrame],
    allowed: Sequence[str],
    now: datetime,
    bar_minutes: int,
) -> dict[str, pd.Series]:
    """Setups on a bar that closed within the last ``FRESH_BAR_MINUTES``."""
    fresh = pd.Timestamp(now.replace(tzinfo=None)) - pd.Timedelta(
        minutes=FRESH_BAR_MINUTES + bar_minutes
    )
    return {
        s: f.iloc[-1]
        for s, f in feats.items()
        if s in allowed and not f.empty and f["setup"].iloc[-1] and f.index[-1] >= fresh
    }


def intraday_entry_window(strategy: IntradayStrategy, now: datetime) -> bool:
    return minute_of_day(now.time()) <= minute_of_day(strategy.config.entry_end) + 5


def pdt_blocked(equity: float) -> Alert | None:
    """Intraday trading needs the pattern-day-trader minimum equity."""
    if equity >= INTRADAY_MIN_EQUITY:
        return None
    return Alert(
        "WARN",
        "No intraday trades: account too small",
        f"Day trading needs at least ${INTRADAY_MIN_EQUITY:,.0f} in the account "
        f"(pattern-day-trader rule); it has ${equity:,.0f}.",
    )


def intraday_kill(
    equity: float, prev_equity: float | None, peak_equity: float | None, risk: RiskConfig
) -> KillState:
    start = prev_equity or equity
    peak = max(peak_equity or start, start)
    return kill_switch(equity, start, peak, risk)


@dataclass(frozen=True)
class IntradayCandidate:
    symbol: str
    win_prob: float
    lower_bound: float
    bar: pd.Series
    features: dict[str, Any]


def intraday_candidates(
    *,
    today: date,
    setups: Mapping[str, pd.Series],
    strategy: str,
    paper_trial: bool,
    held: set[str],
    entered_today: set[str],
    gate: EvidenceGate,
    risk: RiskConfig,
) -> tuple[list[IntradayCandidate], list[SignalRecord]]:
    """Gate today's fresh setups; rank by win rate then score. Returns the ranked
    candidates and the rejected signals (in the order they were decided)."""
    rejected: list[SignalRecord] = []
    ranked = []
    for symbol, row in setups.items():
        sig = row["signature"]
        feats = {"strategy": strategy, "mode": "intraday", "bar": str(row.name),
                 "close": round(float(row["close"]), 2)}  # fmt: skip
        if paper_trial:
            feats["paper_trial"] = True
        if symbol in held or symbol in entered_today:
            rejected.append(SignalRecord(today, symbol, sig, None, None, False,
                                         "already held or entered today", feats))  # fmt: skip
            continue
        g = gate.gate(sig, pd.Timestamp(today), risk)
        if not g.passed:
            rejected.append(SignalRecord(today, symbol, sig, _num(g.win_rate),
                                         _num(g.lower_bound), False, g.reason, feats))  # fmt: skip
            continue
        cand = IntradayCandidate(symbol, g.win_rate, g.lower_bound, row, feats)
        ranked.append((-g.win_rate, -float(row["score"]), symbol, cand))
    ranked.sort(key=lambda c: c[:3])
    return [c[3] for c in ranked], rejected


@dataclass(frozen=True)
class FlattenTarget:
    """A position at flatten time: sold at market, or ``keep`` (opened before today)."""

    symbol: str
    row: PositionRow | None
    qty: int
    keep: bool


def flatten_targets(
    positions: Mapping[str, BrokerPosition],
    rows: Mapping[str, PositionRow],
    pending_sells: set[str],
    today: date,
) -> list[FlattenTarget]:
    """Positions opened today are sold; older ones keep their stop (and are alerted).
    Positions with a market sell already working are skipped."""
    out = []
    for symbol, bp in positions.items():
        row = rows.get(symbol)
        if row is None or row.entry_date < today:
            out.append(FlattenTarget(symbol, row, bp.qty, keep=True))
        elif symbol not in pending_sells:
            out.append(FlattenTarget(symbol, row, bp.qty, keep=False))
    return out


def flatten_cid(today: date, symbol: str, now: datetime, first_done: bool) -> str:
    first = f"{today}-{symbol}-exit-flatten"
    return f"{today}-{symbol}-r{now:%H%M}-exit-flatten" if first_done else first


# ================================================================= overnight


@dataclass(frozen=True)
class OvernightBuy:
    """One overnight pick: buy in today's closing auction, sell at the next open."""

    symbol: str
    rank: int
    limit: float
    stop: float
    cid: str
    sector: str


def overnight_picks(
    bars: Mapping[str, pd.DataFrame], prev: date, today: date, cfg: Any
) -> list[str]:
    """Symbols to buy at today's close, best first, from bars through ``prev`` (the
    previous session): the closing bar isn't final when the order must go in.

    Only ``night_filter`` values that need no same-day close are allowed live."""
    from bot.overnight_hold import pick_symbols

    if cfg.night_filter not in OVERNIGHT_FILTERS:
        raise ValueError(f"night_filter {cfg.night_filter!r} needs today's close; live use "
                         f"one of {sorted(OVERNIGHT_FILTERS)}")  # fmt: skip
    if cfg.night_filter == "notfri" and today.weekday() == 4:
        return []
    past = {s: b.loc[: pd.Timestamp(prev)] for s, b in bars.items()}
    have = {s: b for s, b in past.items() if len(b) and b.index[-1].date() == prev}
    if not have:
        return []
    return pick_symbols(have, pd.Timestamp(prev), replace(cfg, night_filter="all"))


def overnight_buy(symbol: str, rank: int, price: float, today: date, sector: str) -> OvernightBuy:
    """Late-day limit price and the stop attached to the buy."""
    if not price > 0:
        raise ValueError(f"{symbol}: no valid price")
    limit = round(price * (1 + OVERNIGHT_LIMIT_PCT), 2)
    stop = round(limit * (1 - OVERNIGHT_STOP_PCT), 2)
    cid = f"{today}-{symbol}{OVERNIGHT_ENTRY_SUFFIX}"
    return OvernightBuy(symbol, rank, limit, stop, cid, sector)


@dataclass(frozen=True)
class OvernightSell:
    """Sell a position bought before today: at the open, or at market if that missed."""

    row: PositionRow
    qty: int
    cid: str


def overnight_sells(
    positions: Mapping[str, BrokerPosition],
    rows: Mapping[str, PositionRow],
    open_orders: Sequence[BrokerOrder],
    today: date,
    suffix: str = OVERNIGHT_SELL_SUFFIX,
) -> list[OvernightSell]:
    """Every position opened before today without a sell already working. Positions
    opened today (this evening's buys) are kept."""
    selling = {o.symbol for o in flatten_orders(open_orders)
               if o.side == "sell" and o.type not in STOP_TYPES}  # fmt: skip
    out = []
    for symbol, bp in positions.items():
        row = rows.get(symbol)
        if row is None or row.entry_date >= today or symbol in selling:
            continue
        out.append(OvernightSell(row, bp.qty, f"{today}-{symbol}{suffix}"))
    return out
