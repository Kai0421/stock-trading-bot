# Author: Kai0421
"""Deterministic job runner: the bot's whole order path (no LLM anywhere in here).

Jobs (America/New_York, trading days; docs/04-automation.md):

| Job | When | Does |
|---|---|---|
| premarket | 08:30 | reconcile, protect, refresh bars, gate setups, save ranked plans |
| entries | 09:45 | yesterday's exit decisions, then OTO entries (fresh cash check each) |
| manage | 10:00-15:30 every 30 min | reconcile, protect, cancel entries at 11:00, raise stops |
| eod | 16:15 | equity, kill switches, exits/stops from today's close, state.json, pack |
| usage_watch | 17:52 (Sat 09:52) | Claude usage alerts + AI gate (no Claude call) |
| review | 18:07 | if the gate passes: fire the daily review routine with today's pack |
| weekly_report / weekly_review | Sat 09:00 / 10:07 | weekly dashboard; gated AI review |
| refresh_evidence | Sat 11:00 | rerun the backtest when its evidence is 70+ days old |

That is swing mode (``TRADING_MODE=swing``, the default). In intraday mode
(``TRADING_MODE=intraday``, bot.intraday.live, docs/17-intraday.md) ``entries`` and
``manage`` are replaced by ``scan`` (every 5 minutes) and ``flatten`` (15:50), and nothing
is held overnight. In overnight mode (``TRADING_MODE=overnight``, bot.overnight_live,
docs/19-overnight-hold.md) they are replaced by ``open_sell`` (09:00), ``open_check``
(09:35) and ``close_buy`` (15:50).

Three layers (docs/19-layers.md), and this module only wires them together:

- data (``bot.gather``): every read, from the broker, market data, alt-data, the
  evidence file and the journal's book, returned as plain snapshots;
- decision (``bot.decide``): pure functions from snapshots to plans, signals and order
  intents; the pluggable strategies and the risk rules live here;
- execution (``bot.execute``): the only broker writes, plus recording orders and fills.

Every job: file lock -> calendar/clock check -> reconcile DB with broker -> act ->
record in ``runs`` -> alert on failure. ``tick`` (run every 5 minutes by launchd, or Task
Scheduler on Windows) runs whichever job is due, once per slot, so it is robust to sleep,
DST and the machine's time zone. Client order ids are deterministic
(``{date}-{symbol}-{intent}``), so reruns never duplicate orders.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from bot import decide
from bot.backtest import prepare
from bot.broker import Broker, BrokerError
from bot.calendar import MarketCalendar, Session
from bot.config import ConfigError, Settings, load_env_file
from bot.decide import ENTRY_CUTOFF
from bot.evidence import DEFAULT_EVIDENCE_PATH, TRADABLE_VERDICTS
from bot.execute import Executor
from bot.filelock import try_lock
from bot.gather import Gatherer
from bot.host import HostError, host_mismatch
from bot.intraday.live import IntradayJobs, IntradayLoader
from bot.journal import Journal
from bot.memory import build_state, write_state
from bot.notice_check import check_plan, check_sales
from bot.notify import Level, job_name, plain_reason, plan_table, sale_table, sentence, usd
from bot.overnight_live import OvernightJobs
from bot.pack import cleanup_packs, daily_pack, write_pack
from bot.sentiment import DEFAULT_DIR as DEFAULT_SENTIMENT_DIR
from bot.slack_format import bullets, section
from bot.strategies import Strategy, get_strategy, strategy_config, strategy_for
from bot.universe import BENCHMARK, UNIVERSE, universe_for

log = logging.getLogger("bot.engine")
ET = ZoneInfo("America/New_York")
# Jobs whose messages are reports, not trading: they go to the general Slack channel.
GENERAL_JOBS = frozenset({"usage_watch", "review", "weekly_report", "weekly_review"})

JOBS = (
    "premarket",
    "entries",
    "manage",
    "eod",
    "usage_watch",
    "review",
    "weekly_report",
    "weekly_review",
    "refresh_evidence",
    "scan",
    "flatten",
    "open_sell",
    "open_check",
    "close_buy",
)
HISTORY_DAYS = 400  # calendar days of bars for SMA200 / momentum warm-up
# Evidence goes stale at 90 days (bot.evidence); refreshing from 70 leaves three Saturdays
# to retry a failed run before the gate stops new swing trades.
REFRESH_EVIDENCE_AFTER = timedelta(days=70)
BACKTEST_START = date(2016, 1, 1)  # the README's standard backtest window


class Engine(IntradayJobs, OvernightJobs):
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        journal: Journal,
        notifier: Any,
        calendar: MarketCalendar,
        load_bars: Callable[[Sequence[str], date, date | datetime], dict[str, pd.DataFrame]],
        latest_prices: Callable[[Sequence[str]], dict[str, float]],
        clock: Callable[[], datetime],
        universe: Mapping[str, str] = UNIVERSE,
        evidence_path: Path = DEFAULT_EVIDENCE_PATH,
        memory_dir: Path = Path("memory"),
        lock_path: Path = Path("data/engine.lock"),
        altdata: Callable[[str, date], dict[str, bool]] | None = None,
        load_intraday: IntradayLoader | None = None,
        sentiment_dir: Path = DEFAULT_SENTIMENT_DIR,
        backtest_runner: Callable[[Sequence[str]], int] | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.notifier = notifier
        self.calendar = calendar
        self.load_bars = load_bars
        self.latest_prices = latest_prices
        self.clock = clock
        self.universe = dict(universe)
        self.evidence_path = evidence_path
        self.memory_dir = memory_dir
        self.lock_path = lock_path
        self.altdata = altdata
        self.load_intraday = load_intraday
        self.sentiment_dir = sentiment_dir
        self.backtest_runner = backtest_runner

    # ================================================================= plumbing

    @property
    def strategy(self) -> Strategy:
        """The strategy named by ``settings.strategy_name`` (the ``STRATEGY`` setting)."""
        return strategy_for(self.settings)

    @property
    def data(self) -> Gatherer:
        """Data layer: every read (bot.gather)."""
        return Gatherer(
            self.broker, self.journal, self.load_bars, self.latest_prices,
            self.load_intraday, self.altdata, self.evidence_path, self.sentiment_dir,
        )  # fmt: skip

    @property
    def exe(self) -> Executor:
        """Execution layer: every broker write and the bookkeeping of fills (bot.execute)."""
        return Executor(self.broker, self.journal, self.calendar, self._alert, self.universe)

    def _now(self) -> datetime:
        return self.clock().astimezone(ET)

    _category = "bot"  # Slack channel for this run's alerts; reports use "general"

    def _alert(self, level: Level, title: str, text: str = "") -> None:
        log.log(logging.ERROR if level is Level.CRITICAL else logging.INFO, "%s: %s", title, text)
        if self._category == "bot":
            self.notifier.notify(level, title, text)
        else:
            self.notifier.notify(level, title, text, category=self._category)

    def _send(self, alert: decide.Alert) -> None:
        self._alert(Level[alert.level], alert.title, alert.text)

    def run(self, job: str) -> str:
        """Run one job under the file lock; record it; alert on failure. Never raises."""
        if job not in JOBS:
            raise ValueError(f"unknown job {job!r}")
        self._category = "general" if job in GENERAL_JOBS else "bot"
        rescue = getattr(self.notifier, "rescue", None)
        if callable(rescue):  # queued critical alerts nobody sent are sent now
            rescue()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lock:
            if not try_lock(lock):
                log.warning("another job holds the lock; skipping %s", job)
                return "locked"
            run_id = self.journal.start_run(job, self._now())
            try:
                getattr(self, job)()
            except Exception as exc:
                log.exception("job %s failed", job)
                self.journal.finish_run(run_id, "error", self._now(), error=str(exc))
                self._alert(
                    Level.WARN,
                    f"The {job_name(job)} step failed",
                    "The bot skipped it and will try again at the next scheduled run. "
                    f"Technical detail: {exc}",
                )
                return "error"
            self.journal.finish_run(run_id, "ok", self._now())
            return "ok"

    def _session(self, day: date) -> Session | None:
        return self.calendar.session_for(day)

    def _sector(self, symbol: str) -> str:
        return self.universe.get(symbol, "")

    # ================================================================= reconcile / protect

    def _reconcile(self, now: datetime) -> None:
        """Book fills from the broker into the journal (execution layer)."""
        self.exe.reconcile(now)

    def _protect(self, now: datetime) -> None:
        """Every position must have broker-side stops covering all its shares (rule 6)."""
        book = self.data.book()
        self.exe.protect(
            decide.protect_actions(book.positions, book.open_orders, book.rows, now), now
        )

    def _buy_block_reason(self, today: date) -> str | None:
        f = self.data.flags()
        return decide.buy_block_reason(f.paused, f.halt_today, f.halt_day, today)

    # ================================================================= premarket

    def premarket(self) -> None:
        now = self._now()
        today = now.date()
        session = self._session(today)
        if session is None:
            log.info("%s is not a trading day", today)
            return
        if self.intraday_mode:
            note = self._intraday_premarket(now, session)
        elif self.overnight_mode:
            note = self._overnight_premarket(now, session)
        else:
            note = self._premarket(now, session)
        self._premarket_summary(today, note)

    def _premarket_summary(self, today: date, note: str | None) -> None:
        """Daily heartbeat: what premarket found and planned (or why it planned nothing)."""
        signals = self.journal.signals(today, today)
        plans = self.journal.plans(today)
        mode = "dry run, no real orders" if self.settings.dry_run else "paper trading"
        if self.settings.paper_trial:
            mode += f", paper trial of {self.settings.strategy_name}"
        if self.intraday_mode:
            mode += f", intraday ({self.settings.strategy_name})"
        if self.overnight_mode:
            mode += ", overnight (buy at the close, sell at the open)"
        blocks: list[str] = [
            bullets(
                [
                    f"Found {len(signals)} possible trade{'s' if len(signals) != 1 else ''}",
                    f"Planning {len(plans)} buy{'s' if len(plans) != 1 else ''}",
                    f"Mode: {mode}",
                ]
            )
        ]
        if signals:
            blocks.append(section("Buy: possible trades today", plan_table(signals, plans)))
        problems: list[str] = []
        if not (self.intraday_mode or self.overnight_mode):
            rows = list(self.journal.positions().values())
            sales = sale_table(rows)
            blocks.append(
                section("Sell: planned sales (at today's open)", sales)
                if sales
                else "*Sell:* nothing planned today."
            )
            risk = self.settings.risk
            # the same truth the planner used: what the broker says is held
            at_broker = [
                SimpleNamespace(symbol=sym, sector=self._sector(sym))
                for sym in self.data.book().positions
            ]
            problems = [
                *check_plan(plans, at_broker, risk.max_open_positions, risk.max_per_sector),
                *check_sales(rows),
            ]
        if problems:  # the plan breaks its own rules: say so instead of sending it as fine
            blocks.insert(
                0,
                ":warning: *An automatic check found a problem with this plan*\n"
                + bullets(problems)
                + "\nThe order code applies its own limits regardless.",
            )
        grouped: Counter[str] = Counter()
        for reason, n in self.journal.rejection_counts(today, today).items():
            grouped[re.sub(r" [\d.]+% below", " below", reason)] += n  # drop per-setup rates
        if grouped:
            blocks.append(
                section(
                    "Skipped",
                    bullets(
                        [
                            f"{n} setup{'s' if n != 1 else ''} because {plain_reason(r)}"
                            for r, n in grouped.most_common()
                        ]
                    ),
                )
            )
        if note:
            blocks.append(section("Note", bullets([note])))
        self._alert(
            Level.WARN if problems else Level.INFO,
            f":sunrise: Morning plan for {short_day(today)}",
            "\n\n".join(b for b in blocks if b),
        )

    def _record_signals(self, signals: Sequence[decide.SignalRecord]) -> None:
        for r in signals:
            self.journal.record_signal(
                r.day, r.symbol, r.signature, r.win_prob, r.lower_bound, 0, r.taken, r.reason,
                r.features,
            )  # fmt: skip

    def _premarket(self, now: datetime, session: Session) -> str | None:
        """Plan today's entries: gather -> decide -> record. Returns why nothing was
        planned, if it stopped early."""
        today = now.date()
        self._reconcile(now)
        self._protect(now)
        self.journal.save_plans(today, [])
        data = self.data
        strategy = self.strategy
        # Data: flags, bars through the previous session, the strategy's evidence.
        paused = data.flags().paused
        prev = self.calendar.previous_session(today).day
        start = prev - timedelta(days=HISTORY_DAYS)
        bars = {} if paused else data.daily_bars(self.universe, start, prev)
        evidence, path = data.evidence(strategy.name, self.settings.universe_name)
        # Decision: can we plan at all today?
        blocker = decide.premarket_blocker(
            paused, bars.get(BENCHMARK), prev, evidence, path, strategy.name,
            session.is_early_close, self.settings.universe_name,
        )  # fmt: skip
        if blocker is not None:
            note, alert = blocker
            if alert is not None:
                self._send(alert)
            log.info("no plans: %s", note)
            return note
        assert evidence is not None
        gate = data.gate(evidence, self.settings.paper_trial)
        features, tables = prepare(bars, strategy)
        book = data.book()
        held = decide.held_symbols(book.positions, book.rows, book.open_orders)
        wanted = decide.altdata_wanted(
            decide.setups_on(tables, prev), held, self.settings.earnings_policy
        )
        altdata = data.altdata(wanted, today, self.settings.altdata_features)
        result = decide.plan_entries(
            today=today,
            prev=prev,
            strategy=strategy,
            features=features,
            tables=tables,
            held=held,
            open_slots=decide.free_slots(self.settings.risk, book.positions, book.open_orders),
            gate=gate,
            risk=self.settings.risk,
            sectors=self.universe,
            earnings_policy=self.settings.earnings_policy,
            paper_trial=self.settings.paper_trial,
            altdata=altdata,
            altdata_features=self.settings.altdata_features,
            open_sectors=[self._sector(sym) for sym in held],
        )
        # Record the decision (the plan is executed by the entries job).
        self._record_signals(result.signals)
        self.journal.save_plans(today, result.plans)
        log.info("planned %d entries for %s", len(result.plans), today)
        return None

    # ================================================================= entries

    def _execute_exits(self, now: datetime) -> set[str]:
        """Send the exits decided at the previous close (pending_exit on the journal row).

        Returns the symbols sold from, so no second exit is sent for the same shares."""
        data, exe = self.data, self.exe
        book = data.book()
        sent: set[str] = set()
        for intent in decide.exit_intents(book.rows, book.positions, now.date()):
            if data.order_exists(intent.cid):
                continue
            exe.exit(intent, now)
            sent.add(intent.row.symbol)
        return sent

    def _execute_sentiment_exits(self, now: datetime, already_sold: set[str]) -> None:
        """Opt-in (SENTIMENT_EXIT): sell held stocks with a fresh sell warning (docs/20).

        Skips a stock already sold from in this run or with a sell order working (other than
        its stop), so the same shares are never sold twice."""
        if not self.settings.sentiment_exit:
            return
        data, exe = self.data, self.exe
        book = data.book()
        selling = already_sold | {
            o.symbol
            for o in decide.flatten_orders(book.open_orders)
            if o.side == "sell" and o.type not in decide.STOP_TYPES
        }
        rows = {s: r for s, r in book.rows.items() if s not in selling}
        snaps = data.sentiment(list(book.positions), now, self.settings.sentiment_max_age_min)
        for intent in decide.sentiment_exits(rows, book.positions, snaps, now.date()):
            if data.order_exists(intent.cid):
                continue
            exe.exit(intent, now)

    def entries(self) -> None:
        now = self._now()
        today = now.date()
        if self.settings.trading_mode != "swing":
            log.info("entries is a swing-mode job; TRADING_MODE is %s", self.settings.trading_mode)
            return
        if not self.calendar.in_order_window(now) or not self.broker.clock().is_open:
            log.info("outside the order window")
            return
        self._reconcile(now)
        self._protect(now)
        self._execute_exits(now)
        if now.time() >= ENTRY_CUTOFF or not self.calendar.entries_allowed(now):
            return
        blocked = self._buy_block_reason(today)
        if blocked:
            log.info("no entries: %s", blocked)
            return
        if not self.exe.verify_cash_only():
            return
        strategy = self.strategy
        data = self.data
        whitelist = frozenset(self.universe)
        committed_cost: list[float] = []
        committed_sectors: list[str] = []
        for plan in self.journal.plans(today):
            symbol = plan.symbol
            cid = f"{today}-{symbol}-entry"
            if data.order_exists(cid):
                continue
            if symbol in self.broker.positions() or symbol in self.journal.positions():
                continue
            reason = decide.instrument_problem(symbol, whitelist) or data.asset_problem(symbol)
            if reason:
                log.warning("skip %s: %s", symbol, reason)
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
                limit, stop = decide.entry_prices(strategy, price, plan.atr)
            except ValueError as exc:
                log.warning("skip %s: %s", symbol, exc)
                continue
            placed = self._approve_and_submit(
                symbol, limit, stop, plan.sector or self._sector(symbol), cid, plan.win_prob,
                now, committed_cost, committed_sectors,
            )  # fmt: skip
            if placed is None:
                return

    def _approve_and_submit(
        self,
        symbol: str,
        limit: float,
        stop: float,
        sector: str,
        cid: str,
        win_prob: float,
        now: datetime,
        committed_cost: list[float] | None = None,
        committed_sectors: list[str] | None = None,
    ) -> bool | None:
        """Fresh account fetch (data), ``approve_buys`` sizing (decision), then the OTO
        entry with its stop (execution).

        True if an order was placed (or would be, in a dry run), False if not approved,
        None if the account fetch failed or the account is blocked (stop placing orders).
        ``committed_*`` track dry-run orders, which never show up as open orders."""
        committed_cost = [] if committed_cost is None else committed_cost
        committed_sectors = [] if committed_sectors is None else committed_sectors
        try:
            fresh = self.data.fresh()  # fresh before every order (docs/01)
        except BrokerError as exc:
            self._alert(
                Level.WARN,
                "Couldn't reach the broker, so no orders were placed",
                f"Technical detail: {exc}",
            )
            return None
        if fresh.snap.blocked:
            self._alert(
                Level.CRITICAL,
                "Broker account is blocked",
                f"Account status: {fresh.snap.status}. No orders were placed.",
            )
            return None
        order, decision = decide.size_entry(
            symbol=symbol,
            limit=limit,
            stop=stop,
            sector=sector,
            cid=cid,
            win_prob=win_prob,
            snap=fresh.snap,
            open_orders=fresh.open_orders,
            positions=fresh.positions,
            sector_of=self.universe,
            unsettled=self.journal.unsettled_total(now.date()),
            committed_cost=committed_cost,
            committed_sectors=committed_sectors,
            risk=self.settings.risk,
        )
        if order is None:
            log.info("entry %s not approved: %s", symbol, decision.reason)
            if decision.critical:
                self._alert(
                    Level.CRITICAL, f"Didn't buy {symbol}", sentence(plain_reason(decision.reason))
                )
            return False
        if self.exe.submit_entry(order, now) is None:
            # Dry run: nothing appears in open orders, so track it here.
            committed_cost.append(order.cost)
            committed_sectors.append(sector)
        return True

    # ================================================================= manage

    def manage(self) -> None:
        now = self._now()
        if self.settings.trading_mode != "swing":
            log.info("manage is a swing-mode job; TRADING_MODE is %s", self.settings.trading_mode)
            return
        if not self.calendar.is_open(now) or not self.broker.clock().is_open:
            return
        self._reconcile(now)
        self._protect(now)
        exe = self.exe
        orders = self.broker.open_orders()
        for o in decide.entries_to_cancel(orders, now):
            exe.cancel_entry(o, now)
            self._alert(
                Level.INFO,
                f"Cancelled the {o.symbol} buy order",
                "It hadn't filled by 11:00, so the bot cancelled it.",
            )
        positions = self.broker.positions()
        for change in decide.stop_changes(self.journal.positions(), orders, positions):
            exe.change_stop(change)
        if self.calendar.in_order_window(now):
            sold = self._execute_exits(now)
            self._execute_sentiment_exits(now, sold)

    # ================================================================= eod

    def eod(self) -> None:
        now = self._now()
        today = now.date()
        session = self._session(today)
        if session is None:
            return
        self._reconcile(now)
        self._protect(now)
        snap = self.broker.account()
        equity, cash = snap.account.equity, snap.account.cash
        prev = self.journal.previous_equity(today)
        self.journal.record_equity(today, equity, cash)
        peak = max(self.journal.peak_equity() or equity, equity)
        ks = decide.eod_kill(equity, prev, peak, self.settings.risk)
        if ks.halt_new_orders_today:
            next_day = self.calendar.next_session(today).day
            self.journal.set_flag("halt_today", True, ", ".join(ks.reasons), now)
            self.journal.set_kv("halt_day", next_day.isoformat())
            self._alert(
                Level.CRITICAL,
                "Safety stop: big daily loss",
                f"{', '.join(ks.reasons)}. No new orders on {short_day(next_day)}.",
            )
        if ks.paused and not self.journal.flag("paused"):
            self.journal.set_flag("paused", True, ", ".join(ks.reasons), now)
            self._alert(
                Level.CRITICAL,
                "Safety stop: bot paused after a drawdown",
                "The account fell too far from its peak. The bot won't trade until you resume it.",
            )
        if cash < 0:
            self.journal.set_flag("paused", True, "negative cash (margin used)", now)
            self._alert(
                Level.CRITICAL,
                "Cash went negative",
                f"Cash is {usd(cash)}. Buying is paused until you check the account.",
            )

        anomalies: list[str] = []
        rows = self.journal.positions()
        if rows and self.intraday_mode:
            for symbol in rows:  # flatten should have left nothing; stops stay in place
                anomalies.append(f"{symbol}: held overnight in intraday mode; manage by hand")
        elif rows and self.overnight_mode:
            stale = [s for s, r in rows.items() if r.entry_date < today]
            if stale:  # bought tonight is expected; anything older missed its open sell
                anomalies.append(f"held since before today: {', '.join(sorted(stale))}")
        elif rows:
            # Data: today's bars (free plan: no SIP data from the last 15 minutes) and
            # sessions held; decision: the strategy's exits and stops for tomorrow.
            end = now - timedelta(minutes=16)
            bars = self.data.daily_bars(list(rows), today - timedelta(days=HISTORY_DAYS), end)
            held = {
                s: len(self.calendar.trading_days(r.entry_date, today)) - 1 for s, r in rows.items()
            }
            updated, missing_bars = decide.manage_positions(rows, bars, self.strategy, today, held)
            anomalies += missing_bars
            for row in updated:
                self.journal.upsert_position(row)

        expected = {
            "swing": {"premarket", "entries", "manage"},
            "intraday": {"premarket", "scan", "flatten"},
            "overnight": {"premarket", "open_sell", "open_check", "close_buy"},
        }[self.settings.trading_mode]
        if self.overnight_mode and session.is_early_close:
            expected.discard("close_buy")
        missing = sorted(expected - self.journal.jobs_completed_on(today))
        if missing:
            anomalies.append(
                "these steps didn't run today: " + ", ".join(job_name(m) for m in missing)
            )
            self._alert(
                Level.WARN,
                "Some steps didn't run today",
                "These scheduled steps didn't run: "
                + ", ".join(job_name(m) for m in missing)
                + ".",
            )
        if self.settings.routine_daily_url:
            prev_day = self.calendar.previous_session(today).day
            fired = self.journal.get_kv(f"review_fired:{prev_day}") is not None
            if fired and not self.review_exists(prev_day):
                self._alert(
                    Level.INFO,
                    "The AI review didn't happen",
                    f"No review was found for {prev_day}.",
                )

        shadow = self._shadow(today, anomalies)
        self._write_memory(now, equity, prev, peak, anomalies, shadow)

    def _shadow(self, today: date, anomalies: list[str]) -> list[str]:
        """Grade logged signals on bars through the previous session (bot.shadow,
        docs/18). Analysis only: a failure is an anomaly, never a failed EOD."""
        from bot import shadow

        prev_day = self.calendar.previous_session(today).day
        try:
            shadow.settle(
                self.journal,
                self.load_bars,
                self.settings.risk,
                prev_day,
                lambda name: get_strategy(name, strategy_config(name, self.settings)),
            )
            return shadow.pack_lines(shadow.shadow_rows(self.journal))
        except Exception as exc:  # never let analysis break the EOD job
            log.exception("shadow settle failed")
            anomalies.append(f"shadow settle failed: {exc}")
            return []

    def _write_memory(
        self,
        now: datetime,
        equity: float,
        prev: float | None,
        peak: float,
        anomalies: list[str],
        shadow: Sequence[str] = (),
    ) -> None:
        today = now.date()
        rows = list(self.journal.positions().values())
        prices = {s: p.current_price for s, p in self.broker.positions().items()}
        flags = {
            "kill_switch": self.journal.flag("halt_today"),
            "paused": self.journal.flag("paused"),
        }
        state = build_state(now, equity, peak, flags, rows, prices, self.journal.last_runs())
        write_state(self.memory_dir / "state.json", state)

        day_pnl = equity - prev if prev else 0.0
        trades_today = [t for t in self.journal.trades(since=today)]
        # Real fills only: the orders table keeps submit/cancel status, never "filled".
        # Buys: positions (open or closed today) entered today, at the broker fill price.
        # Sells: full exits booked today (partial sells are not dated in the journal).
        fills = [
            {"symbol": p.symbol, "side": "buy", "qty": p.shares_initial, "price": p.entry_price}
            for p in rows
            if p.entry_date == today
        ] + [
            {"symbol": t["symbol"], "side": "buy", "qty": t["shares"], "price": t["entry_price"]}
            for t in trades_today
            if t["entry_date"] == today.isoformat()
        ] + [
            {"symbol": t["symbol"], "side": "sell", "qty": t["shares"], "price": t["exit_price"]}
            for t in trades_today
        ]  # fmt: skip
        exits = [
            {"symbol": t["symbol"], "qty": t["shares"], "price": t["exit_price"], "pnl": t["pnl"],
             "r": t["r_multiple"], "reason": t["exit_reason"]}
            for t in trades_today
        ]  # fmt: skip
        health = [
            {"signature": s, "n": len(o[-30:]), "win_rate": sum(o[-30:]) / len(o[-30:])}
            for s, o in self.journal.outcomes_by_signature().items()
        ]
        text = daily_pack(
            day=today,
            account={
                "equity": equity,
                "day_pnl": day_pnl,
                "day_pct": day_pnl / prev if prev else 0.0,
                "drawdown": 1 - equity / peak if peak else 0.0,
            },
            fills=fills,
            exits=exits,
            positions=rows,
            prices=prices,
            rejections=self.journal.rejection_counts(today, today),
            gate_health=health,
            flags=flags,
            anomalies=anomalies,
            shadow=shadow,
        )
        packs = self.memory_dir / "packs"
        write_pack(packs, f"daily-{today}.md", text)
        cleanup_packs(packs, today)
        summary = (
            f"Account value {usd(equity)} ({'up' if day_pnl >= 0 else 'down'} "
            f"{usd(abs(day_pnl))} today), {(1 - equity / peak) * 100:.1f}% below its peak.\n"
            f"Holding: {', '.join(r.symbol for r in rows) or 'nothing'}. "
            f"Sold today: {len(exits)}.\n"
            f"Warnings: {', '.join(k for k, v in flags.items() if v) or 'none'}"
            + (f"\nNeeds a look: {'; '.join(anomalies)}" if anomalies else "")
        )
        self._alert(Level.INFO, f":bar_chart: End of day {short_day(today)}", summary)

    # ================================================================= AI jobs

    def _usage(self, due: datetime | None):
        from bot.usage import evaluate, read_snapshot

        return evaluate(read_snapshot(self.settings.claude_usage_snapshot), self._now(), due)

    def usage_watch(self) -> None:
        now = self._now()
        due = (
            now.replace(hour=18, minute=7) if now.weekday() < 5 else now.replace(hour=10, minute=7)
        )
        result = self._usage(due)
        for alert in result.alerts:
            if self.journal.once(alert.key):
                self._alert(alert.level, "Claude usage", alert.text)

    def _fire(self, url: str, token: str, pack: Path, what: str) -> None:
        from bot.usage import fire_routine

        if not (url and token):
            log.info("%s routine not configured; skipping", what)
            return
        result = self._usage(None)
        if not result.gate_ok:
            self._alert(
                Level.INFO,
                f"Skipped the AI {what}: usage limit",
                sentence(f"Claude usage is too high: {plain_reason(result.gate_reason)}"),
            )
            return
        if not pack.exists():
            self._alert(Level.INFO, f"Skipped the AI {what}", f"Its input file {pack} is missing.")
            return
        if fire_routine(url, token, pack.read_text()):
            self.journal.set_kv(f"{what.replace(' ', '_')}_fired:{self._now().date()}", "1")
        else:
            self._alert(
                Level.INFO, f"The AI {what} didn't start", "Starting the Claude routine failed."
            )

    def review(self) -> None:
        today = self._now().date()
        pack = self.memory_dir / "packs" / f"daily-{today}.md"
        self._fire(
            self.settings.routine_daily_url, self.settings.routine_daily_token, pack, "review"
        )

    def weekly_report(self) -> None:
        from bot.report import run_weekly_report

        run_weekly_report(self)

    def weekly_review(self) -> None:
        y, w, _ = self._now().isocalendar()
        pack = self.memory_dir / "packs" / f"weekly-{y}-W{w:02d}.md"
        s = self.settings
        self._fire(s.routine_weekly_url, s.routine_weekly_token, pack, "weekly review")

    def refresh_evidence(self) -> None:
        """Rerun the swing backtest when the gate's evidence is getting old.

        Same command as a manual run (README), so the new verdict decides trading under the
        unchanged gate: a rejected run stops new swing trades. The owner's stability advisory
        (decisions.md 2026-10-05) is kept only when the current evidence was made with it."""
        if self.intraday_mode or self.overnight_mode:
            return  # intraday has its own backtest; overnight has no evidence gate
        name, universe = self.strategy.name, self.settings.universe_name
        today = self._now().date()
        evidence, path = self.data.evidence(name, universe)
        if evidence is not None and today - evidence.as_of < REFRESH_EVIDENCE_AFTER:
            return
        if evidence is None or evidence.strategy != name or evidence.universe != universe:
            # never overwrite another strategy's or universe's file
            own = name if universe == "sp100" else f"{name}-{universe}"
            path = path.with_name(f"{own}.json")
        argv = [
            "--start", BACKTEST_START.isoformat(),
            "--end", (today - timedelta(days=1)).isoformat(),
            "--strategy", name,
            "--universe", universe,
            "--stability",
            "--out", str(self.memory_dir / "backtests"),
            "--evidence-out", str(path),
        ]  # fmt: skip
        if evidence is not None and "stability advisory" in evidence.verdict:
            argv.append("--stability-advisory")
        runner = self.backtest_runner
        if runner is None:
            from bot.backtest import main as runner
        code = runner(argv)
        fresh, _ = self.data.evidence(name, universe)
        if fresh is None or (evidence is not None and fresh.as_of <= evidence.as_of):
            raise RuntimeError(f"backtest exited with code {code} and wrote no new evidence")
        if fresh.verdict in TRADABLE_VERDICTS:
            self._alert(
                Level.INFO,
                "Backtest refreshed",
                f"{name} evidence now runs to {fresh.as_of} ({fresh.verdict}); "
                "swing trading carries on.",
            )
        else:
            self._alert(
                Level.WARN,
                "Backtest refreshed but not accepted",
                f"The new {name} backtest to {fresh.as_of} was {fresh.verdict}, so the bot "
                "will open no new swing trades. Open positions keep their stops.",
            )

    def review_exists(self, day: date) -> bool:
        """The daily review for ``day``: a local journal file or the routine's branch."""
        if (self.memory_dir / "journal" / f"{day}.md").exists():
            return True
        try:
            out = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", f"claude/review-{day}"],
                capture_output=True, text=True, timeout=20, check=False,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            return True  # can't tell: don't raise a false alarm
        return out.returncode != 0 or bool(out.stdout.strip())

    def resume(self) -> None:
        self.journal.set_flag("paused", False, "manual resume", self._now())
        self._alert(Level.INFO, "Bot resumed", "Trading is back on.")

    # ================================================================= scheduler

    def _schedule(self, day: date) -> list[tuple[str, time, time]]:
        if day.weekday() == 5:  # Saturday
            jobs = [
                ("weekly_report", time(9, 0), time(23, 59)),
                ("usage_watch", time(9, 52), time(10, 6)),
                ("weekly_review", time(10, 7), time(14, 0)),
            ]
            if self.settings.auto_backtest:
                jobs.append(("refresh_evidence", time(11, 0), time(23, 59)))
            return jobs
        session = self._session(day)
        if session is None:
            return []
        if self.intraday_mode:
            return self._intraday_schedule(day, session)
        if self.overnight_mode:
            return self._overnight_schedule(day, session)
        close = session.close.astimezone(ET)
        last_manage = time(12, 30) if session.is_early_close else time(15, 30)
        jobs = [("premarket", time(8, 30), time(9, 40)), ("entries", time(9, 45), time(10, 59))]
        slot = datetime.combine(day, time(10, 0))
        while slot.time() <= last_manage:
            jobs.append(("manage", slot.time(), (slot + timedelta(minutes=29)).time()))
            slot += timedelta(minutes=30)
        eod_start = (close + timedelta(minutes=15)).time()
        jobs += [
            ("eod", eod_start, time(23, 59)),
            ("usage_watch", time(17, 52), time(18, 6)),
            ("review", time(18, 7), time(21, 0)),
        ]
        return jobs

    def tick(self) -> list[str]:
        """Run every job whose window contains now and whose slot hasn't run yet."""
        now = self._now()
        ran = []
        for job, start, end in self._schedule(now.date()):
            slot = f"{now.date()}T{start:%H:%M}"
            if start <= now.time() <= end and not self.journal.slot_done(job, slot):
                self.run(job)
                self.journal.mark_slot(job, slot)
                ran.append(job)
        return ran

    # ================================================================= wiring

    @classmethod
    def from_env(cls) -> Engine:
        from bot.altdata import make_provider
        from bot.calendar import AlpacaCalendarSource
        from bot.data import (
            BarCache,
            latest_prices,
            load_daily_bars,
            load_intraday_bars,
            make_data_client,
        )
        from bot.notify import make_notifier

        settings = Settings.from_env({**load_env_file(Path(".env")), **os.environ})
        broker = Broker.from_settings(settings)
        journal = Journal(settings.db_path)
        data_client = make_data_client(settings)
        cache = BarCache()

        def load_bars(symbols: Sequence[str], start: date, end: date | datetime) -> dict:
            use_cache = None if isinstance(end, datetime) else cache
            return load_daily_bars(data_client, symbols, start, end, use_cache).bars

        return cls(
            settings=settings,
            broker=broker,
            journal=journal,
            notifier=make_notifier(settings, journal),
            calendar=MarketCalendar(AlpacaCalendarSource(broker._c)),
            load_bars=load_bars,
            latest_prices=lambda s: latest_prices(data_client, s, settings.live_data_feed),
            clock=lambda: datetime.now(ET),
            universe=universe_for(settings.universe_name),
            altdata=make_provider(settings, journal, data_client),
            # Intraday mode: real-time IEX 5-minute bars (free plan; SIP is 15 min late).
            load_intraday=lambda syms, start, end: (
                load_intraday_bars(
                    data_client, syms, start, end, 5, settings.live_data_feed, batch=50
                ).bars
            ),
        )


def short_day(day: date) -> str:
    """'Mon Oct 5'. strftime's no-padding day flag (percent, dash, d) doesn't exist on Windows."""
    return f"{day:%a %b} {day.day}"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.engine")
    ap.add_argument("command", choices=[*JOBS, "tick", "resume", "status"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        problem = host_mismatch({**load_env_file(Path(".env")), **os.environ})
    except HostError as exc:
        problem = str(exc)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    try:
        engine = Engine.from_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.command == "tick":
        engine.tick()
        return 0
    if args.command == "resume":
        engine.resume()
        return 0
    if args.command == "status":
        j = engine.journal
        print({"flags": {k: v.value for k, v in j.flags().items()}, "last_runs": j.last_runs(),
               "positions": list(j.positions())})  # fmt: skip
        return 0
    return 0 if engine.run(args.command) == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
