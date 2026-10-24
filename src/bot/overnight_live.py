# Author: Kai0421
"""Overnight jobs for the engine (``TRADING_MODE=overnight``; docs/19-overnight-hold.md).

Buy just before the close, sell in the next opening auction. Paper only, by the owner's
decision (memory/decisions.md 2026-10-08): the win-rate gate is waived for this mode
(its backtest wins about half its nights), and a broker-side stop cannot protect the
gap at the open. Everything else stays: the fresh account fetch and ``approve_buys``
cash and sizing checks before every order, the cash-only lock, the instrument checks,
the kill switches, and a broker-side stop on every position once it has filled.

Jobs (America/New_York, trading days), replacing swing mode's entries and manage:

- ``premarket`` (08:30): reconcile, protect, heartbeat.
- ``open_sell`` (09:31-09:34): for every position bought before today, cancel its stop and
  sell at market, once the market is open. Not a market-on-open order: Alpaca's expired
  unfilled on this account (2026-10-13) and the sale came 7 minutes late. The stop stays
  in place until this sale, so the position is protected in the first minutes too.
- ``open_check`` (09:35-10:30): book the fills; anything still held from before today is
  sold at market (the backup if ``open_sell`` missed its tick).
- ``close_buy`` (10 to 4 minutes before the close): pick the symbols from bars through
  the previous session, then a sized DAY limit buy (up to 1% above the last price) for
  each, with its stop (5% under the limit) attached. Unfilled, it expires at the close.
  Not a limit-on-close order: Alpaca paper let those expire below their limit
  (2026-10-09). No buys before an early close.
- ``eod`` (close + 15 minutes): books the fills and swaps each new position's DAY stop
  for a GTC one, then equity, kill switches and memory as in swing mode.

Like the other modes these only wire the three layers together (docs/19-layers.md).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from bot import decide
from bot.decide import OVERNIGHT_LATE_SUFFIX
from bot.journal import PlanRow
from bot.notify import Level, plain_reason, sentence
from bot.overnight_hold import NAME, OvernightConfig

if TYPE_CHECKING:
    from bot.calendar import Session

log = logging.getLogger("bot.engine")

OVERNIGHT_HISTORY_DAYS = 120  # calendar days: enough for 20 nights of momentum
# Backtest win rate of the live rules (docs/19-overnight-hold.md, Oct 2021-Oct 2026). Not
# a gate (owner waiver); recorded with each trade so the reviews can compare.
OVERNIGHT_WIN_PROB = 0.50
CHIP_SECTOR = "Information Technology"
# 15:50-15:56 on a normal day: late enough to buy close to the closing price, and the
# window is longer than the 5-minute tick so one tick always lands in it.
BUY_BEFORE_CLOSE = (timedelta(minutes=10), timedelta(minutes=4))


class OvernightJobs:
    """Mixin for ``bot.engine.Engine``; uses its layers, calendar and helpers."""

    # Provided by Engine.
    settings: Any
    broker: Any
    journal: Any
    calendar: Any
    universe: dict[str, str]
    data: Any
    exe: Any

    @property
    def overnight_mode(self) -> bool:
        return self.settings.trading_mode == "overnight"

    @property
    def overnight_config(self) -> OvernightConfig:
        return OvernightConfig()

    def overnight_sectors(self) -> dict[str, str]:
        """The universe's sectors plus the overnight basket's (all chip makers)."""
        return {**{s: CHIP_SECTOR for s in self.overnight_config.symbols}, **self.universe}

    def _overnight_schedule(self, day: date, session: Session) -> list[tuple[str, time, time]]:
        from bot.engine import ET

        close = session.close.astimezone(ET).replace(tzinfo=None)
        start, end = (close - d for d in BUY_BEFORE_CLOSE)
        return [
            ("premarket", time(8, 30), time(8, 59)),
            ("open_sell", time(9, 31), time(9, 34)),
            ("open_check", time(9, 35), time(10, 30)),
            ("close_buy", start.time(), end.time()),
            ("eod", (close + timedelta(minutes=15)).time(), time(23, 59)),
            ("usage_watch", time(17, 52), time(18, 6)),
            ("review", time(18, 7), time(21, 0)),
        ]

    # ----------------------------------------------------------- premarket

    def _overnight_premarket(self, now: datetime, session: Session) -> str | None:
        self._reconcile(now)
        self._protect(now)
        self.journal.save_plans(now.date(), [])
        cfg = self.overnight_config
        # All the basket's stocks share one sector, so the per-sector limit caps the night.
        most = min(cfg.max_positions, self.settings.risk.max_per_sector)
        notes = [
            f"overnight mode: buy up to {most} of {', '.join(cfg.symbols)} at "
            f"the close, sell at the next open (no win-rate gate: owner waiver)"
        ]
        if self.journal.flag("paused"):
            notes.append("bot is paused (drawdown kill switch or manual); resume to trade")
        if session.is_early_close:
            notes.append("early close today: no overnight buys")
        held = sorted(r.symbol for r in self.journal.positions().values())
        if held:
            notes.append(f"to sell at the open: {', '.join(held)}")
        return "; ".join(notes)

    # ----------------------------------------------------------- open

    def open_sell(self) -> None:
        now = self._now()
        if not self.overnight_mode:
            log.info("open_sell is an overnight-mode job; TRADING_MODE is %s",
                     self.settings.trading_mode)  # fmt: skip
            return
        today = now.date()
        if self._session(today) is None:
            return
        if not self.calendar.is_open(now) or not self.broker.clock().is_open:
            return  # a market sale needs the market open; the stop keeps protecting until then
        self._reconcile(now)
        book = self.data.book()
        for sell in decide.overnight_sells(book.positions, book.rows, book.open_orders, today):
            if self.data.order_exists(sell.cid):
                continue
            self.exe.sell_all(decide.IntradayExit(sell.row, sell.qty, sell.cid), now)

    def open_check(self) -> None:
        now = self._now()
        if not self.overnight_mode:
            return
        if not self.calendar.is_open(now) or not self.broker.clock().is_open:
            return
        today = now.date()
        self._reconcile(now)
        self._protect(now)
        book = self.data.book()
        late = decide.overnight_sells(
            book.positions, book.rows, book.open_orders, today, OVERNIGHT_LATE_SUFFIX
        )
        for sell in late:
            if self.data.order_exists(sell.cid):
                continue
            self._alert(Level.WARN, f"{sell.row.symbol} didn't sell at the open",
                        "The 09:31 sale didn't go through, so selling it at the "
                        "market price now.")  # fmt: skip
            self.exe.sell_all(decide.IntradayExit(sell.row, sell.qty, sell.cid), now)

    # ----------------------------------------------------------- close

    def close_buy(self) -> None:
        now = self._now()
        if not self.overnight_mode:
            log.info("close_buy is an overnight-mode job; TRADING_MODE is %s",
                     self.settings.trading_mode)  # fmt: skip
            return
        today = now.date()
        session = self._session(today)
        if session is None or session.is_early_close:
            return
        if not self.calendar.is_open(now) or not self.broker.clock().is_open:
            return
        self._reconcile(now)
        self._protect(now)
        blocked = self._buy_block_reason(today)
        if blocked:
            log.info("no overnight buys: %s", blocked)
            return
        cfg = self.overnight_config
        data = self.data
        prev = self.calendar.previous_session(today).day
        bars = data.daily_bars(list(cfg.symbols), prev - timedelta(days=OVERNIGHT_HISTORY_DAYS),
                               prev)  # fmt: skip
        picks = decide.overnight_picks(bars, prev, today, cfg)
        if not picks:
            log.info("no overnight picks for %s", today)
            return
        if not self.exe.verify_cash_only():
            return
        book = data.book()
        held = decide.held_symbols(book.positions, book.rows, book.open_orders)
        sectors = self.overnight_sectors()
        whitelist = frozenset(sectors)
        committed_cost: list[float] = []
        committed_sectors: list[str] = []
        plans: list[PlanRow] = []
        for rank, symbol in enumerate(picks, start=1):
            features = {"strategy": NAME, "rank": rank, "flags": ["owner waiver: no win-rate gate"]}
            reason = None
            if symbol in held:
                reason = "already held or ordered"
            elif data.order_exists(f"{today}-{symbol}{decide.OVERNIGHT_ENTRY_SUFFIX}"):
                reason = "already ordered today"
            else:
                reason = decide.instrument_problem(symbol, whitelist) or data.asset_problem(symbol)
            price = None if reason else data.price(symbol)
            if not reason and price is None:
                reason = "no price"
            if reason:
                self._record_overnight(today, symbol, False, reason, features)
                continue
            assert price is not None
            buy = decide.overnight_buy(symbol, rank, price, today, sectors.get(symbol, ""))
            placed = self._approve_close_buy(buy, now, committed_cost, committed_sectors)
            if placed is None:
                return
            outcome = "bought at the close" if placed else "not approved"
            self._record_overnight(today, symbol, placed, outcome, features)
            if placed:
                plans.append(PlanRow(today, rank, symbol, NAME, OVERNIGHT_WIN_PROB, 0.0, 0.0,
                                     buy.sector, price))  # fmt: skip
        self.journal.save_plans(today, plans)

    def _record_overnight(
        self, today: date, symbol: str, taken: bool, reason: str, features: dict[str, Any]
    ) -> None:
        self._record_signals([decide.SignalRecord(today, symbol, NAME, OVERNIGHT_WIN_PROB, None,
                                                  taken, reason, features)])  # fmt: skip

    def _approve_close_buy(
        self,
        buy: decide.OvernightBuy,
        now: datetime,
        committed_cost: list[float],
        committed_sectors: list[str],
    ) -> bool | None:
        """Fresh account fetch, ``approve_buys`` sizing and cash checks, then the
        late-day buy. Same contract as the engine's ``_approve_and_submit``."""
        from bot.broker import BrokerError

        try:
            fresh = self.data.fresh()
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
            symbol=buy.symbol,
            limit=buy.limit,
            stop=buy.stop,
            sector=buy.sector,
            cid=buy.cid,
            win_prob=OVERNIGHT_WIN_PROB,
            snap=fresh.snap,
            open_orders=fresh.open_orders,
            positions=fresh.positions,
            sector_of=self.overnight_sectors(),
            unsettled=self.journal.unsettled_total(now.date()),
            committed_cost=committed_cost,
            committed_sectors=committed_sectors,
            risk=self.settings.risk,
        )
        if order is None:
            log.info("overnight buy %s not approved: %s", buy.symbol, decision.reason)
            if decision.critical:
                self._alert(
                    Level.CRITICAL,
                    f"Didn't buy {buy.symbol}",
                    sentence(plain_reason(decision.reason)),
                )
            return False
        if self.exe.submit_close_buy(buy, order.shares, OVERNIGHT_WIN_PROB, now) is None:
            committed_cost.append(order.cost)
            committed_sectors.append(buy.sector)
        return True
