# Author: Kai0421
"""Intraday jobs for the engine (``TRADING_MODE=intraday``; docs/17-intraday.md).

Deterministic Python only, like the rest of the order path. ``bot.engine.Engine`` mixes
these in and runs them from ``tick`` instead of swing mode's entries/manage jobs:

- ``premarket`` (08:30): reconcile, protect, check the strategy's evidence, heartbeat.
- ``scan`` (every 5 minutes from 09:35 until the flatten time): reconcile, protect, exits
  and stop raises for today's positions, cancel stale entries, then new gated entries
  on the last completed bar.
- ``flatten`` (``flatten_at``, 15:50, or 10 minutes before an early close, then every
  5 minutes): cancel unfilled entries, sell every position opened today.
- ``eod`` (close + 15 minutes): equity, kill switches, memory; nothing should be held.

Like the swing jobs these only wire the three layers together (docs/19-layers.md):
reads through ``self.data`` (bot.gather), decisions from ``bot.decide`` (the intraday
strategy is called only there) and orders through ``self.exe`` (bot.execute).

Entries go through the same ``approve_buys`` sizing and cash checks, with a fresh
account fetch before every order, and every entry is an OTO order carrying its
broker-side stop. Positions opened before today (left over from swing mode) are never
sold by ``flatten``: they keep their stop and raise an alert.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from bot import decide
from bot.broker import BrokerError
from bot.decide import INTRADAY_ENTRY_SUFFIX as ENTRY_SUFFIX
from bot.evidence import EvidenceGate
from bot.intraday.base import IntradayStrategy
from bot.intraday.strategies import get_intraday_strategy
from bot.notify import Level
from bot.universe import BENCHMARK

if TYPE_CHECKING:
    from bot.calendar import Session

log = logging.getLogger("bot.engine")

DAILY_HISTORY_DAYS = 400
FLATTEN_EARLY_CLOSE = timedelta(minutes=10)

IntradayLoader = Callable[[Sequence[str], datetime, datetime], dict[str, pd.DataFrame]]


class IntradayJobs:
    """Mixin for ``bot.engine.Engine``; uses its layers, calendar and helpers."""

    # Provided by Engine.
    settings: Any
    broker: Any
    journal: Any
    calendar: Any
    universe: dict[str, str]
    data: Any
    exe: Any

    # ----------------------------------------------------------- helpers

    @property
    def intraday_mode(self) -> bool:
        return self.settings.trading_mode == "intraday"

    @property
    def intraday_strategy(self) -> IntradayStrategy:
        return get_intraday_strategy(self.settings.strategy_name)

    def _flatten_time(self, session: Session) -> time:
        from bot.engine import ET

        close = session.close.astimezone(ET)
        early = (close - FLATTEN_EARLY_CLOSE).time()
        return min(self.intraday_strategy.config.flatten_at, early)

    def _intraday_schedule(self, day: date, session: Session) -> list[tuple[str, time, time]]:
        from bot.engine import ET

        flatten = self._flatten_time(session)
        close = session.close.astimezone(ET)
        jobs = [("premarket", time(8, 30), time(9, 34))]
        slot = datetime.combine(day, time(9, 35))
        end = datetime.combine(day, flatten)
        while slot < end:
            jobs.append(("scan", slot.time(), (slot + timedelta(minutes=4)).time()))
            slot += timedelta(minutes=5)
        while slot < close.replace(tzinfo=None) - timedelta(minutes=1):
            jobs.append(("flatten", slot.time(), (slot + timedelta(minutes=4)).time()))
            slot += timedelta(minutes=5)
        jobs += [
            ("eod", (close + timedelta(minutes=15)).time(), time(23, 59)),
            ("usage_watch", time(17, 52), time(18, 6)),
            ("review", time(18, 7), time(21, 0)),
        ]
        return jobs

    def _evidence_gate(self, strategy: IntradayStrategy) -> tuple[EvidenceGate | None, str]:
        evidence, path = self.data.evidence(strategy.name)
        if evidence is None:
            return None, f"no backtest evidence ({path}); run python -m bot.intraday.backtest"
        if evidence.strategy != strategy.name:
            return None, f"evidence is for {evidence.strategy}, not {strategy.name}"
        return self.data.gate(evidence, self.settings.paper_trial), ""

    # ----------------------------------------------------------- premarket

    def _intraday_premarket(self, now: datetime, session: Session) -> str | None:
        self._reconcile(now)
        self._protect(now)
        self.journal.save_plans(now.date(), [])
        strategy = self.intraday_strategy
        notes = [f"intraday mode, {strategy.name}, flat by {self._flatten_time(session):%H:%M}"]
        if self.journal.flag("paused"):
            notes.append("bot is paused (drawdown kill switch or manual); resume to trade")
        if session.is_early_close:
            notes.append("early close today: no new entries")
        gate, why = self._evidence_gate(strategy)
        if gate is None:
            self._alert(Level.WARN, "No intraday trades today: no usable backtest", why)
            notes.append(why)
        old = [r.symbol for r in self.journal.positions().values() if r.entry_date < now.date()]
        if old:
            notes.append(f"held from before today (not flattened, stops stay): {', '.join(old)}")
        return "; ".join(notes)

    # ----------------------------------------------------------- scan

    def scan(self) -> None:
        now = self._now()
        if not self.intraday_mode:
            log.info("scan is an intraday-mode job; TRADING_MODE is swing")
            return
        if not self.calendar.is_open(now) or not self.broker.clock().is_open:
            return
        today = now.date()
        session = self._session(today)
        self._reconcile(now)
        self._protect(now)
        strategy = self.intraday_strategy
        data = self.data
        rows = {s: r for s, r in self.journal.positions().items() if r.entry_date == today}
        for o in decide.stale_intraday_entries(self.broker.open_orders(), now):
            self.exe.cancel_entry(o, now)
            log.info("cancelled stale intraday entry %s", o.symbol)
        allowed = strategy.symbols or tuple(self.universe)
        symbols = sorted(set(allowed) | set(rows) | {BENCHMARK})
        bars = data.today_bars(symbols, now, strategy.bar_minutes)
        prev = self.calendar.previous_session(today).day
        daily = data.daily_bars(symbols, prev - timedelta(days=DAILY_HISTORY_DAYS), prev)
        feats, warnings = decide.intraday_features(strategy, bars, daily)
        for w in warnings:
            log.warning(w)
        self._intraday_exits(now, strategy, rows, feats)
        flatten = self._flatten_time(session) if session else time(15, 50)
        if now.time() < flatten:
            self._intraday_entries(now, strategy, feats, allowed)

    def _intraday_exits(
        self,
        now: datetime,
        strategy: IntradayStrategy,
        rows: dict[str, Any],
        feats: dict[str, pd.DataFrame],
    ) -> None:
        today = now.date()
        data = self.data
        entry_bars = {s: self.journal.get_kv(f"ientry:{today}:{s}") for s in rows}
        book = data.book()
        actions = decide.intraday_manage(
            rows, book.positions, book.open_orders, feats, strategy, entry_bars, today
        )
        for action in actions:
            if isinstance(action, decide.IntradayExit):
                if data.order_exists(action.cid):
                    continue
                self.exe.sell_all(action, now)
            else:
                self.exe.raise_stop(*action)

    def _intraday_entries(
        self,
        now: datetime,
        strategy: IntradayStrategy,
        feats: dict[str, pd.DataFrame],
        allowed: Sequence[str],
    ) -> None:
        today = now.date()
        if not self.calendar.entries_allowed(now):
            return
        if not decide.intraday_entry_window(strategy, now):
            return
        blocked = self._buy_block_reason(today)
        if blocked:
            log.info("no intraday entries: %s", blocked)
            return
        setups = decide.intraday_setups(feats, allowed, now, strategy.bar_minutes)
        if not setups:
            return
        data = self.data
        try:
            snap = self.broker.account()
        except BrokerError as exc:
            self._alert(
                Level.WARN, "Couldn't reach the broker, so no orders were placed", f"Error: {exc}"
            )
            return
        pdt = decide.pdt_blocked(snap.account.equity)
        if pdt is not None:
            if self.journal.once(f"pdt_block:{today}"):
                self._send(pdt)
            return
        ks = decide.intraday_kill(
            snap.account.equity, self.journal.previous_equity(today),
            self.journal.peak_equity(), self.settings.risk,
        )  # fmt: skip
        if ks.blocks_buys:
            if ks.halt_new_orders_today and self.journal.once(f"intraday_halt:{today}"):
                self.journal.set_flag("halt_today", True, ", ".join(ks.reasons), now)
                self.journal.set_kv("halt_day", today.isoformat())
                self._alert(Level.CRITICAL, "Safety stop: big loss today",
                            f"{', '.join(ks.reasons)}. No new trades today.")  # fmt: skip
            return
        gate, why = self._evidence_gate(strategy)
        if gate is None:
            log.info("no intraday entries: %s", why)
            return
        book = data.book()
        held = decide.held_symbols(book.positions, book.rows, book.open_orders)
        entered = {s for s in setups if self.journal.has_order(f"{today}-{s}{ENTRY_SUFFIX}")}
        candidates, rejected = decide.intraday_candidates(
            today=today,
            setups=setups,
            strategy=strategy.name,
            paper_trial=self.settings.paper_trial,
            held=held,
            entered_today=entered,
            gate=gate,
            risk=self.settings.risk,
        )
        self._record_signals(rejected)
        if candidates and not self.exe.verify_cash_only():
            return
        whitelist = frozenset(self.universe)
        for c in candidates:
            symbol = c.symbol
            bad = decide.instrument_problem(symbol, whitelist) or data.asset_problem(symbol)
            if bad:
                log.warning("skip %s: %s", symbol, bad)
                continue
            price = data.price(symbol)
            if price is None:
                self._alert(
                    Level.WARN,
                    f"No price for {symbol}",
                    "Couldn't get a current price, so the bot didn't buy it.",
                )
                continue
            try:
                limit, stop = decide.entry_prices(strategy, price, c.bar.to_dict())
            except ValueError as exc:
                log.warning("skip %s: %s", symbol, exc)
                continue
            if price <= stop:
                continue
            cid = f"{today}-{symbol}{ENTRY_SUFFIX}"
            placed = self._approve_and_submit(
                symbol, limit, stop, self._sector(symbol), cid, c.win_prob, now
            )
            self._record_signals([
                decide.SignalRecord(today, symbol, c.bar["signature"], c.win_prob,
                                    c.lower_bound, placed,  # type: ignore[arg-type]
                                    "entered" if placed else "not approved", c.features)
            ])  # fmt: skip
            if placed:
                self.journal.set_kv(f"ientry:{today}:{symbol}", str(c.bar.name))

    # ----------------------------------------------------------- flatten

    def flatten(self) -> None:
        now = self._now()
        if not self.intraday_mode:
            log.info("flatten is an intraday-mode job; TRADING_MODE is swing")
            return
        if not self.calendar.is_open(now) or not self.broker.clock().is_open:
            return
        today = now.date()
        data, exe = self.data, self.exe
        self._reconcile(now)
        for o in self.broker.open_orders():
            if o.side == "buy" and o.client_order_id.endswith(ENTRY_SUFFIX):
                exe.cancel_entry(o, now)
        self._protect(now)
        rows = self.journal.positions()
        pending_sells = {
            o.symbol for o in self.broker.open_orders() if o.side == "sell" and o.type == "market"
        }
        targets = decide.flatten_targets(self.broker.positions(), rows, pending_sells, today)
        for t in targets:
            if t.keep:
                if self.journal.once(f"overnight_hold:{today}:{t.symbol}"):
                    self._alert(
                        Level.CRITICAL,
                        f"{t.symbol} was left open",
                        "It was bought before today (swing mode or by hand), so the bot "
                        "didn't sell it. Its stop-loss stays; please manage it yourself.",
                    )
                continue
            assert t.row is not None
            done = data.order_exists(f"{today}-{t.symbol}-exit-flatten")
            cid = decide.flatten_cid(today, t.symbol, now, done)
            exe.sell_all(decide.IntradayExit(t.row, t.qty, cid), now)
