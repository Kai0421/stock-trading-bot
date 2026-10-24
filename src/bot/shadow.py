# Author: Kai0421
"""Shadow settlement: grade every journalled signal, taken or not, on real daily bars.

The engine logs each setup it sees in the ``signals`` table with its estimated win
probability and whether it was taken (or why not). This module replays each one with the
strategy's own entry, stop and exits, exactly as the backtester's ``simulate_signals``
does, and stores the result in a ``shadow_trades`` table in the same database. That gives:

- **Calibration:** does a setup the gate called 70% really win about 70% of the time
  going forward? (Predicted win probability bucket vs realised win rate.)
- **Blocked-trade counterfactual:** what did the setups each rule blocked (gate, no
  slots, already held, ...) go on to do?

Read-only towards trading: it never touches orders, positions or the gate's live
outcomes (``trades``). Daily (swing) signals only; signals tagged ``mode=intraday`` are
skipped because a daily bar can't replay a 5-minute entry. Idea from bennyjo/phil's
forecast ledger (research/phil-ideas.md in the project files).

Usage: ``python -m bot.shadow [--settle] [--out FILE]``
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from bot.backtest import OpenTrade, _bar, _columns, _entry_prices, slippage_per_side
from bot.config import RiskConfig
from bot.journal import Journal
from bot.strategies import Strategy

HISTORY_DAYS = 400  # calendar days of warm-up before the oldest signal (as the engine)
NOMINAL_SHARES = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_trades (
    signal_id INTEGER PRIMARY KEY, day TEXT NOT NULL, symbol TEXT NOT NULL,
    strategy TEXT NOT NULL, signature TEXT, win_prob REAL, taken INTEGER NOT NULL,
    reason TEXT NOT NULL, entry_date TEXT NOT NULL, exit_date TEXT NOT NULL,
    entry_price REAL NOT NULL, exit_price REAL NOT NULL, r_multiple REAL NOT NULL,
    win INTEGER NOT NULL, exit_reason TEXT NOT NULL
);
"""  # noqa: E501

LoadBars = Callable[[Sequence[str], date, date], Mapping[str, pd.DataFrame]]


def _features_of(signal: Mapping[str, Any]) -> dict[str, Any]:
    try:
        out = json.loads(signal.get("features") or "{}")
    except json.JSONDecodeError:
        return {}
    return out if isinstance(out, dict) else {}


def settle_signal(
    signal: Mapping[str, Any], features: pd.DataFrame, strategy: Strategy, risk: RiskConfig
) -> dict[str, Any] | None:
    """The finished shadow trade for one signal, or None while it can't be graded yet.

    The signal's ``day`` is the session the engine planned to enter on, so the entry is
    that day's open (the backtester's next-open limit entry); 1R uses the ATR recorded
    with the signal. None when the bars don't reach the entry day, the trade is still
    open at the last bar, or the signal has no usable ATR or stop."""
    atr = _features_of(signal).get("atr")
    if not isinstance(atr, int | float) or not atr > 0:
        return None
    idx = features.index
    j0 = int(idx.searchsorted(pd.Timestamp(signal["day"])))
    if j0 >= len(idx) or idx[j0].date() != date.fromisoformat(signal["day"]):
        return None
    cols = _columns(features)
    o, lo = cols["open"], cols["low"]
    slip = slippage_per_side(risk)
    prices = _entry_prices(float(o[j0]), float(atr), strategy, slip)
    if prices is None:
        return None
    _, fill, stop = prices
    t = OpenTrade(
        signal["symbol"],
        signal["signature"] or "",
        idx[j0],
        idx[j0],
        fill,
        stop,
        NOMINAL_SHARES,
        slip,
    )
    for j in range(j0, len(idx)):
        if j > j0:
            t.on_open(o[j])
        t.on_low(lo[j])
        if t.closed:
            trade = t.to_trade(idx[j])
            return {
                "entry_date": idx[j0].date().isoformat(),
                "exit_date": idx[j].date().isoformat(),
                "entry_price": round(fill, 4),
                "exit_price": round(t.proceeds / NOMINAL_SHARES, 4),
                "r_multiple": round(trade.r_multiple, 4),
                "win": int(trade.win),
                "exit_reason": trade.exit_reason,
            }
        t.on_close(_bar(cols, j), strategy)
    return None


def _ensure_table(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def pending_signals(journal: Journal) -> list[dict[str, Any]]:
    """Signals with a setup signature that have no shadow trade yet (daily mode only)."""
    _ensure_table(journal.db)
    rows = journal.db.execute(
        "SELECT * FROM signals WHERE signature IS NOT NULL AND id NOT IN"
        " (SELECT signal_id FROM shadow_trades) ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if _features_of(row).get("mode", "swing") != "intraday":
            out.append(row)
    return out


def settle(
    journal: Journal,
    load_bars: LoadBars,
    risk: RiskConfig,
    today: date,
    strategy_for: Callable[[str], Strategy],
    default_strategy: str = "trend_pullback",
) -> int:
    """Grade every pending signal that has finished by ``today``. Returns how many."""
    pending = [s for s in pending_signals(journal) if s["day"] <= today.isoformat()]
    if not pending:
        return 0
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in pending:
        by_strategy[_features_of(s).get("strategy") or default_strategy].append(s)
    strategies: dict[str, Strategy] = {}
    for name in list(by_strategy):
        try:
            strategies[name] = strategy_for(name)
        except ValueError:
            del by_strategy[name]  # not a daily strategy in this build
    if not by_strategy:
        return 0

    signals = [s for group in by_strategy.values() for s in group]
    start = min(date.fromisoformat(s["day"]) for s in signals) - timedelta(days=HISTORY_DAYS)
    bars = load_bars(sorted({s["symbol"] for s in signals}), start, today)

    done = 0
    for name, group in by_strategy.items():
        strategy = strategies[name]
        features: dict[str, pd.DataFrame] = {}
        for s in group:
            sym = s["symbol"]
            if sym not in features:
                df = bars.get(sym)
                if df is None or df.empty:
                    continue
                features[sym] = strategy.features(df)
            out = settle_signal(s, features[sym], strategy, risk)
            if out is None:
                continue
            journal.db.execute(
                "INSERT OR IGNORE INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], s["day"], sym, name, s["signature"], s["win_prob"], s["taken"],
                 s["reason"], out["entry_date"], out["exit_date"], out["entry_price"],
                 out["exit_price"], out["r_multiple"], out["win"], out["exit_reason"]),
            )  # fmt: skip
            done += 1
    journal.db.commit()
    return done


def shadow_rows(journal: Journal, since: date | None = None) -> list[dict[str, Any]]:
    _ensure_table(journal.db)
    sql, params = "SELECT * FROM shadow_trades", ()
    if since is not None:
        sql, params = sql + " WHERE exit_date >= ?", (since.isoformat(),)
    return [dict(r) for r in journal.db.execute(sql + " ORDER BY exit_date, signal_id", params)]


# ----------------------------------------------------------------- summaries

BUCKETS = ((0.0, 0.55, "<55%"), (0.55, 0.60, "55-60%"), (0.60, 0.65, "60-65%"),
           (0.65, 0.70, "65-70%"), (0.70, 1.01, "70%+"))  # fmt: skip


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    probs = [r["win_prob"] for r in rows if r["win_prob"] is not None]
    return {
        "n": n,
        "predicted": sum(probs) / len(probs) if probs else None,
        "realized": sum(r["win"] for r in rows) / n,
        "win_rate": sum(r["win"] for r in rows) / n,
        "mean_r": sum(r["r_multiple"] for r in rows) / n,
    }


def calibration(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Predicted win probability bucket vs realised win rate (empty buckets dropped)."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        p = r["win_prob"]
        label = "unknown"
        if p is not None:
            label = next(lbl for lo, hi, lbl in BUCKETS if lo <= p < hi)
        groups[label].append(r)
    order = [lbl for _, _, lbl in BUCKETS] + ["unknown"]
    return [{"bucket": b, **_summary(groups[b])} for b in order if groups.get(b)]


def by_reason(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Taken signals vs each rejection reason: what did the blocked setups go on to do?"""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        groups["taken" if r["taken"] else r["reason"]].append(r)
    out = [{"reason": k, **_summary(v)} for k, v in groups.items()]
    return sorted(out, key=lambda x: (x["reason"] != "taken", -x["n"], x["reason"]))


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def render_markdown(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["## Shadow trades (every signal, taken or not)", ""]
    if not rows:
        return "\n".join([*lines, "No shadow trades settled yet.", ""])
    total = _summary(rows)
    lines += [
        f"{total['n']} settled: win rate {_pct(total['win_rate'])}, "
        f"mean {total['mean_r']:+.2f}R. Small samples are noise; judge at n >= 100.",
        "",
        "### Calibration (predicted vs realised win rate)",
        "",
        "| predicted | n | mean predicted | realised | mean R |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {b['bucket']} | {b['n']} | {_pct(b['predicted'])} | {_pct(b['realized'])} "
        f"| {b['mean_r']:+.2f} |"
        for b in calibration(rows)
    ]
    lines += [
        "",
        "### By outcome of the signal (taken vs why it was blocked)",
        "",
        "| signal | n | win rate | mean R |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r['reason']} | {r['n']} | {_pct(r['win_rate'])} | {r['mean_r']:+.2f} |"
        for r in by_reason(rows)
    ]
    return "\n".join([*lines, ""])


def pack_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Compact lines for the daily review pack (empty when nothing has settled)."""
    if not rows:
        return []
    total = _summary(rows)
    out = [f"- all: n={total['n']} win {_pct(total['win_rate'])} {total['mean_r']:+.2f}R"]
    out += [
        f"- calib {b['bucket']}: n={b['n']} predicted {_pct(b['predicted'])} "
        f"realised {_pct(b['realized'])} {b['mean_r']:+.2f}R"
        for b in calibration(rows)
    ]
    out += [
        f"- {r['reason']}: n={r['n']} win {_pct(r['win_rate'])} {r['mean_r']:+.2f}R"
        for r in by_reason(rows)
    ]
    return out


# ----------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    from bot.config import ConfigError, Settings, load_env_file
    from bot.data import ET, BarCache, load_daily_bars, make_data_client
    from bot.strategies import get_strategy, strategy_config

    ap = argparse.ArgumentParser(prog="python -m bot.shadow", description=__doc__.split("\n")[0])
    ap.add_argument("--settle", action="store_true", help="grade new signals (needs data keys)")
    ap.add_argument("--since", type=date.fromisoformat, help="report trades exiting from here")
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    ap.add_argument("--cache", default="data/bars")
    ap.add_argument("--out", help="also write the report to this file")
    args = ap.parse_args(argv)

    if env is None:
        env = {**load_env_file(Path(".env")), **os.environ}
    try:
        settings = Settings.from_env(env)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    journal = Journal(settings.db_path)

    if args.settle:
        try:
            client = make_data_client(settings)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        cache = BarCache(args.cache)

        def load(symbols: Sequence[str], start: date, end: date) -> Mapping[str, pd.DataFrame]:
            return load_daily_bars(client, symbols, start, end, cache, args.feed).bars

        def strategy_for(name: str) -> Strategy:
            return get_strategy(name, strategy_config(name, settings))

        # Bars through yesterday: today's bar is incomplete while the market is open.
        yesterday = datetime.now(ET).date() - timedelta(days=1)
        n = settle(journal, load, settings.risk, yesterday, strategy_for)
        print(f"settled {n} shadow trade(s)", file=sys.stderr)

    text = render_markdown(shadow_rows(journal, args.since))
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
