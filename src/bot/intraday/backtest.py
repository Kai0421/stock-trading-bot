# Author: Kai0421
"""Intraday backtest: 5-minute bars, flat every night, the same gate and risk rules.

Two stages, like the swing backtester (bot.backtest):

1. **Signals** (``simulate_symbol``): every setup of every symbol becomes a one-share
   trade. Entry at the next bar's open plus slippage, a broker-side stop checked on
   every bar's low (filled at the stop or the gap open), the strategy's exits at the
   next bar's open, and a forced exit at ``flatten_at``. One entry per symbol per
   session.
2. **Portfolio** (``run_portfolio``): the walk-forward probability gate (``SignatureBook``:
   only trades that exited before the session started count), ``approve_buys`` sizing
   and cash checks, the settled-cash rule (sale proceeds don't come back until the next
   session), the −3% daily-loss halt (checked intraday too) and the −10% drawdown pause.

Run ``python -m bot.intraday.backtest --start 2016-01-01 --end 2026-09-30``. It writes a
report to ``memory/backtests/intraday-<id>.md`` and per-strategy evidence for the live
gate. Bars are fetched one symbol at a time and dropped after use, so ten years of
5-minute bars for the whole universe fit in memory.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import sys
import time as _time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot.backtest import (
    DEFAULT_REPORT_DIR,
    Metrics,
    SignatureBook,
    Trade,
    _fmt,
    _md_cell,
    _metrics_rows,
    acceptance,
    compute_metrics,
    signature_table,
    slippage_per_side,
    yearly_breakdown,
)
from bot.config import ConfigError, RiskConfig, Settings, load_env_file
from bot.evidence import Evidence, evidence_from_trades, save_evidence
from bot.intraday.base import IntradayPosition, IntradayStrategy, minute_of_day
from bot.intraday.strategies import get_intraday_strategy, intraday_registry
from bot.risk import Account, BuyRequest, KillState, PortfolioState, approve_buys, kill_switch
from bot.universe import BENCHMARK

ET = ZoneInfo("America/New_York")
DAILY_WARMUP_DAYS = 400


@dataclass(frozen=True)
class Signal:
    trade: Trade  # one share
    score: float


@dataclass(frozen=True)
class IntradayBacktestConfig:
    start: date
    end: date
    initial_equity: float = 100_000.0
    gated: bool = True
    resume_after_days: int = 0  # measurement only, as in bot.backtest
    resume_size: float = 0.5


# ------------------------------------------------------------------ signals


def simulate_symbol(
    symbol: str, f: pd.DataFrame, strategy: IntradayStrategy, risk: RiskConfig
) -> list[Signal]:
    """One-share trades for every session's first setup of ``symbol`` (prepared ``f``)."""
    slip = slippage_per_side(risk)
    flatten = minute_of_day(strategy.config.flatten_at)
    setup = f["setup"].to_numpy(dtype=bool)
    if not setup.any():
        return []
    ts = f.index
    day = f["day"].to_numpy()
    minute = f["minute"].to_numpy()
    o, lo, c = (f[k].to_numpy(dtype="float64") for k in ("open", "low", "close"))
    sig = f["signature"].to_numpy(dtype=object)
    score = f["score"].to_numpy(dtype="float64")
    records = f.to_dict("records")
    n = len(f)
    out: list[Signal] = []
    taken: set = set()
    for i in np.flatnonzero(setup):
        d = day[i]
        j = i + 1
        if d in taken or j >= n or day[j] != d or minute[j] >= flatten:
            continue
        taken.add(d)  # one attempt per session, filled or not
        entry = o[j] * (1 + slip)
        try:
            stop = strategy.stop_for(entry, records[i])
        except ValueError:
            continue
        if o[j] <= stop:
            continue  # opened through the stop: the live OTO would not be placed
        pos = IntradayPosition(symbol, entry, stop, stop, 1, 0)
        k, pending, exit_price, reason = j, None, math.nan, ""
        while True:
            if k >= n or day[k] != d:  # early close: out at the last bar's close
                k -= 1
                exit_price, reason = c[k] * (1 - slip), "flatten"
                break
            if pending is not None and k > j:
                exit_price, reason = o[k] * (1 - slip), pending
                break
            if minute[k] >= flatten:
                exit_price, reason = o[k] * (1 - slip), "flatten"
                break
            if lo[k] <= pos.stop:
                exit_price, reason = min(o[k], pos.stop) * (1 - slip), "stop"
                break
            pos = dataclasses.replace(pos, bars_held=k - j + 1)
            pending, new_stop = strategy.manage(pos, records[k])
            pos = dataclasses.replace(pos, stop=new_stop)
            k += 1
        pnl = exit_price - entry
        trade = Trade(
            symbol=symbol,
            signature=sig[i],
            signal_date=ts[i],
            entry_date=ts[j],
            exit_date=ts[k],
            entry_price=entry,
            initial_stop=stop,
            shares=1,
            pnl=pnl,
            r_multiple=pnl / (entry - stop),
            exit_reason=reason,
        )
        out.append(Signal(trade, float(score[i]) if math.isfinite(score[i]) else -math.inf))
    return out


# ------------------------------------------------------------------ portfolio


@dataclass
class PortfolioResult:
    trades: list[Trade]
    equity: pd.Series
    gate_rejections: int = 0
    halted_days: int = 0
    pauses: list[tuple[pd.Timestamp, pd.Timestamp | None]] = field(default_factory=list)
    max_positions: int = 0


def run_portfolio(
    signals: Sequence[Signal],
    sessions: pd.DatetimeIndex,
    sectors: Mapping[str, str],
    risk: RiskConfig,
    bt: IntradayBacktestConfig,
) -> PortfolioResult:
    """Walk-forward portfolio over ``sessions`` (normalized dates)."""
    book = SignatureBook(s.trade for s in signals) if bt.gated else None
    by_day: dict[pd.Timestamp, list[Signal]] = defaultdict(list)
    for s in signals:
        by_day[s.trade.entry_date.normalize()].append(s)
    days = sessions[(sessions >= pd.Timestamp(bt.start)) & (sessions <= pd.Timestamp(bt.end))]
    whitelist = frozenset(sectors)
    res = PortfolioResult(trades=[], equity=pd.Series(dtype="float64"))
    equity = peak = bt.initial_equity
    halt_next = False
    paused, paused_days, recover_to = False, 0, None
    values: list[float] = []
    for d in days:
        start_eq = equity
        realized = 0.0
        spent = 0.0
        halted = halt_next
        open_: list[tuple[pd.Timestamp, str, float]] = []  # exit time, sector, pnl
        cands = sorted(by_day.get(d, ()), key=lambda s: (s.trade.entry_date, -s.score))
        for s in cands:
            t = s.trade
            still = []
            for exit_ts, sector, pnl in open_:
                if exit_ts <= t.entry_date:
                    realized += pnl
                else:
                    still.append((exit_ts, sector, pnl))
            open_ = still
            if paused or halted:
                continue
            if start_eq + realized <= start_eq * (1 - risk.daily_loss_kill_pct) + 0.01:
                halted = True  # intraday daily-loss halt: no more entries today
                continue
            if book is not None and not book.gate(t.signature, d, risk).passed:
                res.gate_rejections += 1
                continue
            eq_now = start_eq + realized
            state = PortfolioState(
                account=Account(start_eq - spent, start_eq - spent, eq_now),
                margin_lock_verified=True,
                unsettled_proceeds=0.0,
                open_buy_orders_cost=0.0,
                open_position_sectors=tuple(sec for _, sec, _ in open_),
                kill=KillState(),
                whitelist=whitelist,
            )
            req = BuyRequest(t.symbol, t.entry_price, t.initial_stop, sectors.get(t.symbol, ""))
            (dec,) = approve_buys([req], state, risk)
            if not dec.approved:
                continue
            shares = dec.shares
            if recover_to is not None:
                shares = math.floor(shares * bt.resume_size)
                if shares < 1:
                    continue
            spent += shares * t.entry_price  # proceeds settle next session (cash rule)
            pnl = shares * t.pnl
            open_.append((t.exit_date, sectors.get(t.symbol, ""), pnl))
            res.trades.append(dataclasses.replace(t, shares=shares, pnl=pnl))
            res.max_positions = max(res.max_positions, len(open_))
        realized += sum(p for _, _, p in open_)
        equity = start_eq + realized
        values.append(equity)
        peak = max(peak, equity)
        if recover_to is not None and equity >= recover_to:
            recover_to = None
        ks = kill_switch(equity, start_eq, peak, risk)
        halt_next = ks.halt_new_orders_today
        res.halted_days += int(halt_next)
        if paused:
            paused_days += 1
            if bt.resume_after_days and paused_days >= bt.resume_after_days:
                res.pauses[-1] = (res.pauses[-1][0], d)
                recover_to = max(recover_to or 0.0, peak)
                peak = equity
                paused = False
        elif ks.paused:
            res.pauses.append((d, None))
            paused, paused_days = True, 0
    res.equity = pd.Series(values, index=days, dtype="float64", name="equity")
    return res


# ------------------------------------------------------------------ results


@dataclass
class IntradayResult:
    strategy: IntradayStrategy
    signals: list[Signal]
    signal_metrics: Metrics
    portfolio: PortfolioResult
    metrics: Metrics
    failures: list[str]
    exit_reasons: Counter
    stability: list[tuple[str, bool, Metrics, tuple[str, ...]]] | None = None

    @property
    def verdict(self) -> str:
        if self.failures:
            return "REJECTED"
        if self.stability is None:
            return "PROVISIONAL (stability check not run)"
        return "ACCEPTED" if all(ok for _, ok, _, _ in self.stability) else "REJECTED (unstable)"


def evaluate(
    strategy: IntradayStrategy,
    signals: Sequence[Signal],
    sessions: pd.DatetimeIndex,
    sectors: Mapping[str, str],
    risk: RiskConfig,
    bt: IntradayBacktestConfig,
) -> IntradayResult:
    signals = sorted(signals, key=lambda s: (s.trade.exit_date, s.trade.symbol))
    start, end = pd.Timestamp(bt.start), pd.Timestamp(bt.end) + pd.Timedelta(days=1)
    window = [s.trade for s in signals if start <= s.trade.exit_date < end]
    p = run_portfolio(signals, sessions, sectors, risk, bt)
    metrics = compute_metrics(p.trades, p.equity)
    return IntradayResult(
        strategy=strategy,
        signals=list(signals),
        signal_metrics=compute_metrics(window, None),
        portfolio=p,
        metrics=metrics,
        failures=acceptance(metrics, risk),
        exit_reasons=Counter(t.exit_reason for t in window),
    )


def perturbations(
    strategy: IntradayStrategy, pct: float = 0.2
) -> list[tuple[str, IntradayStrategy]]:
    """Each stability parameter moved by -pct and +pct; invalid or unchanged skipped."""
    out = []
    for name in strategy.stability_params:
        value = getattr(strategy.config, name)
        for factor in (1 - pct, 1 + pct):
            new = factor * value
            new = round(new) if isinstance(value, int) else round(new, 10)
            if new == value:
                continue
            try:
                cfg = dataclasses.replace(strategy.config, **{name: new})
            except ConfigError:
                continue
            out.append((f"{name} x{factor:g}", strategy.with_config(cfg)))
    return out


def collect_signals(
    strategies: Sequence[IntradayStrategy],
    symbols: Sequence[str],
    load: Callable[[str], tuple[pd.DataFrame | None, pd.DataFrame | None]],
    spy_daily: pd.DataFrame | None,
    risk: RiskConfig,
    log: Callable[[str], None] = print,
) -> tuple[dict[int, list[Signal]], dict[str, tuple[date, date, int]], dict[str, str]]:
    """Signals per strategy (by position in ``strategies``), loading each symbol once.

    Returns the signals, each symbol's (first session, last session, sessions) and the
    symbols skipped with the reason."""
    signals: dict[int, list[Signal]] = defaultdict(list)
    spans: dict[str, tuple[date, date, int]] = {}
    skipped: dict[str, str] = {}
    for n, symbol in enumerate(symbols, 1):
        wanted = [k for k, s in enumerate(strategies) if s.symbols is None or symbol in s.symbols]
        if not wanted:
            continue
        bars, daily = load(symbol)
        if bars is None or bars.empty:
            skipped[symbol] = "no intraday bars"
            continue
        days = pd.DatetimeIndex(bars.index).normalize().unique()
        spans[symbol] = (days[0].date(), days[-1].date(), len(days))
        for k in wanted:
            s = strategies[k]
            try:
                f = s.prepare(bars, daily, spy_daily)
            except ValueError as exc:
                skipped[symbol] = f"{s.name}: {exc}"
                continue
            signals[k].extend(simulate_symbol(symbol, f, s, risk))
        log(f"[{n}/{len(symbols)}] {symbol}: {len(days)} sessions")
    return signals, spans, skipped


# ------------------------------------------------------------------ report


def run_id_for(strategies: Sequence[IntradayStrategy], risk: RiskConfig, bt, today: date) -> str:
    payload = json.dumps(
        [
            [{"name": s.name, **dataclasses.asdict(s.config)} for s in strategies],
            dataclasses.asdict(risk),
            dataclasses.asdict(bt),
        ],
        default=str,
        sort_keys=True,
    )
    return f"intraday-{today:%Y%m%d}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def _summary_row(r: IntradayResult) -> str:
    m, s = r.metrics, r.signal_metrics
    return (
        f"| `{r.strategy.name}` | {s.trades} | {_fmt(s.win_rate)} | {_fmt(s.expectancy_r, 'r')} "
        f"| {m.trades} | {_fmt(m.win_rate)} | {_fmt(m.wilson_lb)} | {_fmt(m.expectancy_r, 'r')} "
        f"| {_fmt(m.cagr)} | {_fmt(m.max_drawdown)} | {r.verdict} |"
    )


def _cost_line(trades: Sequence[Trade], risk: RiskConfig) -> str:
    """Signal-level averages before and after costs, so a cheaper market (or a strategy
    with wider stops) can be judged from the same run."""
    if not trades:
        return "Per trade: no signals."
    slip = slippage_per_side(risk)
    risk_px = np.array([t.entry_price - t.initial_stop for t in trades])
    entry = np.array([t.entry_price for t in trades])
    net = np.array([t.pnl for t in trades])
    exit_px = entry + net
    gross = net + slip * (entry / (1 + slip) + exit_px / (1 - slip))  # add back both sides
    return (
        f"Per trade (signal level): {np.mean(net / entry):+.3%} of price after costs, "
        f"{np.mean(gross / entry):+.3%} before; mean R {np.mean(net / risk_px):+.3f} after, "
        f"{np.mean(gross / risk_px):+.3f} before; stop distance {np.median(risk_px / entry):.2%} "
        "of price (median)."
    )


def render_report(
    results: Sequence[IntradayResult],
    risk: RiskConfig,
    bt: IntradayBacktestConfig,
    run_id: str,
    spans: Mapping[str, tuple[date, date, int]],
    notes: Sequence[str] = (),
) -> str:
    firsts = sorted(v[0] for v in spans.values())
    sessions = sorted(v[2] for v in spans.values())
    lines = [
        f"# Intraday backtest {run_id}",
        "",
        f"- Period: {bt.start} to {bt.end}; initial equity ${bt.initial_equity:,.0f}; "
        "5-minute bars, flat every night",
        f"- Minute history: {len(spans)} symbols; first session {firsts[0] if firsts else 'n/a'}"
        f" (latest first session {firsts[-1] if firsts else 'n/a'}); median "
        f"{sessions[len(sessions) // 2] if sessions else 0} sessions per symbol",
        f"- Probability gate: {'on (walk-forward)' if bt.gated else 'OFF'} "
        f"({risk.gate_mode}); risk per trade {risk.effective_risk_per_trade:.2%}, "
        f"max {risk.max_open_positions} positions, {risk.max_position_pct:.0%} per stock",
        f"- Costs: {risk.slippage_bps_round_trip:g} bps round trip",
        "",
        "| Strategy | Signals | Signal win | Signal mean R | Portfolio trades | Win rate "
        "| Lower bound | Mean R | CAGR | Max DD | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *[_summary_row(r) for r in results],
        "",
    ]
    for r in results:
        p = r.portfolio
        sigs = signature_table([s.trade for s in r.signals])
        lines += [
            f"## `{r.strategy.name}`",
            "",
            r.strategy.description,
            "",
            f"Gate rejections {p.gate_rejections}; daily-loss halts {p.halted_days}; "
            f"drawdown pauses {len(p.pauses)}; most positions at once {p.max_positions}.",
            "",
            "| Metric | Portfolio |",
            "|---|---|",
            *_metrics_rows(r.metrics),
            "",
            *([f"- FAIL: {f}" for f in r.failures] or ["- All acceptance criteria met"]),
            "",
            "Exits: " + ", ".join(f"{k} {v}" for k, v in r.exit_reasons.most_common()),
            "",
            _cost_line([s.trade for s in r.signals], risk),
            "",
            "| Year | Trades | Win rate | Mean R |",
            "|---|---|---|---|",
            *[
                f"| {y} | {int(row.trades)} | {_fmt(row.win_rate)} "
                f"| {_fmt(row.expectancy_r, 'r')} |"
                for y, row in yearly_breakdown([s.trade for s in r.signals]).iterrows()
            ],
            "",
            "| Signature | Trades | Win rate | Lower bound | Mean R |",
            "|---|---|---|---|---|",
            *[
                f"| `{_md_cell(row.signature)}` | {row.trades} | {_fmt(row.win_rate)} "
                f"| {_fmt(row.wilson_lb)} | {_fmt(row.expectancy_r, 'r')} |"
                for row in sigs.head(12).itertuples()
            ],
            "",
        ]
        if r.stability is not None:
            lines += [
                "| Change (±20%) | Passed | Trades | Win rate | Mean R | Max DD |",
                "|---|---|---|---|---|---|",
                *[
                    f"| {label} | {'yes' if ok else 'no'} | {m.trades} | {_fmt(m.win_rate)} "
                    f"| {_fmt(m.expectancy_r, 'r')} | {_fmt(m.max_drawdown)} |"
                    for label, ok, m, _ in r.stability
                ],
                "",
            ]
    lines += [
        "## Caveats",
        "",
        "- The yearly and signature tables are signal level (every setup, one share); the "
        "metric tables are the gated portfolio.",
        "- Survivorship bias: today's S&P 100. Fills are modelled from 5-minute OHLC (next "
        "bar's open plus slippage; stops at the stop or the gap open). Real fills on fast "
        "moves can be worse.",
        "- Backtests use SIP bars; the live bot sees IEX bars, whose highs and lows differ a "
        "little from the consolidated tape.",
        "- Max drawdown is measured on end-of-day equity (positions are flat every night).",
    ]
    if notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI


def _retry(fn: Callable[[], object], attempts: int = 4) -> object:
    for k in range(attempts):
        try:
            return fn()
        except Exception:  # rate limits and network blips on long fetches
            if k == attempts - 1:
                raise
            _time.sleep(2 ** (k + 1))
    raise AssertionError("unreachable")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    from bot.data import BarCache, load_daily_bars, load_intraday_bars, make_data_client
    from bot.universe import sector_of, symbols

    ap = argparse.ArgumentParser(prog="python -m bot.intraday.backtest")
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--strategies", help="comma-separated (default: every intraday strategy)")
    ap.add_argument("--symbols", help="comma-separated subset of the universe")
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--no-gate", action="store_true", help="diagnostics: take every setup")
    ap.add_argument("--resume-after-days", type=int, default=0)
    ap.add_argument("--stability", action="store_true", help="also run the ±20%% check")
    ap.add_argument("--out", default=str(DEFAULT_REPORT_DIR))
    ap.add_argument("--evidence-dir", default="memory/evidence")
    args = ap.parse_args(argv)

    if env is None:
        env = {**load_env_file(Path(".env")), **os.environ}
    try:
        settings = Settings.from_env({**env, "TRADING_MODE": "swing", "STRATEGY": ""})
        client = make_data_client(settings)
        names = (
            [n.strip() for n in args.strategies.split(",") if n.strip()]
            if args.strategies
            else sorted(intraday_registry())
        )
        strategies = [get_intraday_strategy(n) for n in names]
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    risk = settings.risk
    bt = IntradayBacktestConfig(
        args.start, args.end, args.equity, not args.no_gate, args.resume_after_days
    )
    universe = args.symbols.split(",") if args.symbols else symbols()
    if BENCHMARK not in universe:
        universe = [BENCHMARK, *universe]
    daily = load_daily_bars(
        client, universe, args.start - timedelta(days=DAILY_WARMUP_DAYS), args.end, BarCache()
    )
    spy_daily = daily.bars.get(BENCHMARK)
    if spy_daily is None:
        print(f"error: no {BENCHMARK} daily bars", file=sys.stderr)
        return 2
    t0 = datetime.combine(args.start, time(9, 0), tzinfo=ET)
    t1 = datetime.combine(args.end, time(17, 0), tzinfo=ET)

    variants: list[IntradayStrategy] = list(strategies)
    labels: list[tuple[int, str]] = [(k, "") for k in range(len(strategies))]
    if args.stability:
        for k, s in enumerate(strategies):
            for label, v in perturbations(s):
                variants.append(v)
                labels.append((k, label))

    def load(symbol: str):
        got = _retry(lambda: load_intraday_bars(client, [symbol], t0, t1, 5, args.feed))
        return got.bars.get(symbol), daily.bars.get(symbol)  # type: ignore[union-attr]

    signals, spans, skipped = collect_signals(variants, universe, load, spy_daily, risk)
    sessions = pd.DatetimeIndex(spy_daily.index).normalize()
    sectors = {s: sector_of(s) for s in spans}
    results = [
        evaluate(s, signals.get(k, []), sessions, sectors, risk, bt)
        for k, s in enumerate(strategies)
    ]
    if args.stability:
        for r in results:
            r.stability = []
        for vi, (v, (k, label)) in enumerate(zip(variants, labels, strict=True)):
            if not label:
                continue
            res = evaluate(v, signals.get(vi, []), sessions, sectors, risk, bt)
            results[k].stability.append((label, not res.failures, res.metrics,
                                         tuple(res.failures)))  # fmt: skip
    run_id = run_id_for(strategies, risk, bt, datetime.now(ET).date())
    notes = [f"skipped {s}: {why}" for s, why in sorted(skipped.items())]
    text = render_report(results, risk, bt, run_id, spans, notes)
    path = Path(args.out) / f"{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    as_of = pd.Timestamp(bt.end) + pd.Timedelta(days=1)
    for r in results:
        ev = Evidence(
            evidence_from_trades((s.trade for s in r.signals), as_of),
            bt.end,
            run_id,
            r.verdict,
            r.strategy.name,
        )
        save_evidence(Path(args.evidence_dir) / f"{r.strategy.name}.json", ev)
    print(text)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
