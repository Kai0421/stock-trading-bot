# Author: Kai0421
"""Execution layer: turns decisions into broker orders and records what happened.

The only module that writes to the broker (docs/19-layers.md). It takes the intents
``bot.decide`` returns (entries, exits, protective stops, stop changes, cancels), sends
them through ``bot.broker.Broker`` with deterministic client order ids, records every
order in the journal, and books fills (``reconcile``: adopted entries, partial and full
exits, unsettled proceeds). It decides nothing new, except the fail-safe recovery the
hard rules require: if an exit sell fails, the position's stop is put back at once.

In a dry run the broker sends nothing and returns None; orders are still recorded
(status ``dry_run``), so the journal shows what would have happened.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from bot.broker import BrokerError, BrokerOrder, BrokerPosition
from bot.decide import (
    STOP_TYPES,
    EntryOrder,
    ExitIntent,
    IntradayExit,
    OvernightBuy,
    OvernightSell,
    ProtectAction,
    StopChange,
    adopted_stop,
    exit_reason,
    flatten_orders,
    open_stops,
)
from bot.journal import PlanRow, PositionRow
from bot.notify import Level, reason_text, shares, usd
from bot.slack_format import bullets

log = logging.getLogger("bot.engine")
ET = ZoneInfo("America/New_York")


STOP_SWAPPED = " The old stop-loss was cancelled first because this sale replaces it."


class Executor:
    """Broker writes and journal bookkeeping. ``alert(level, title, text)`` notifies."""

    def __init__(
        self,
        broker: Any,
        journal: Any,
        calendar: Any,
        alert: Callable[[Level, str, str], None],
        sector_of: Mapping[str, str],
    ) -> None:
        self.broker = broker
        self.journal = journal
        self.calendar = calendar
        self.alert = alert
        self.sector_of = sector_of

    def record(
        self,
        order: BrokerOrder | None,
        cid: str,
        symbol: str,
        side: str,
        qty: float,
        type_: str,
        now: datetime,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> None:
        status = order.status if order else ("dry_run" if self.broker.dry_run else "unknown")
        self.journal.record_order(
            cid, order.id if order else None, symbol, side, qty, type_, status, now,
            limit_price, stop_price,
        )  # fmt: skip

    # ============================================================= fills (reconcile)

    def reconcile(self, now: datetime) -> None:
        """Journal <- broker: adopt filled entries, book partial and full exits."""
        positions = self.broker.positions()
        orders = self.broker.open_orders()
        rows = self.journal.positions()
        for symbol, bp in positions.items():
            if symbol not in rows:
                self._adopt(bp, orders, now)
        for symbol, row in rows.items():
            bp = positions.get(symbol)
            qty = bp.qty if bp else 0
            if qty == row.shares:
                continue
            if qty > row.shares:
                self.alert(
                    Level.CRITICAL,
                    f"{symbol}: share count doesn't match",
                    f"The broker shows {shares(qty)} but the bot expected {row.shares}. "
                    "Please check this position by hand.",
                )
                continue
            self._book_sells(row, qty, now)

    def _adopt(self, bp: BrokerPosition, orders: list[BrokerOrder], now: datetime) -> None:
        symbol = bp.symbol
        stops = open_stops(orders).get(symbol, [])
        entry = self.journal.latest_order(symbol, "buy")
        plan: PlanRow | None = None
        entry_day = now.date()
        if entry is not None:
            entry_day = date.fromisoformat(entry["client_order_id"][:10])
            plan = next((p for p in self.journal.plans(entry_day) if p.symbol == symbol), None)
        else:
            self.alert(
                Level.CRITICAL,
                f"{symbol}: position the bot didn't open",
                f"Found {shares(bp.qty)} of {symbol} that the bot didn't buy. "
                "It's adding a stop-loss to protect it; please check it by hand.",
            )
        initial = adopted_stop(
            stops, entry.get("stop_price") if entry is not None else None, bp.avg_entry_price
        )
        self.journal.upsert_position(
            PositionRow(
                symbol=symbol,
                entry_price=bp.avg_entry_price,
                shares=bp.qty,
                shares_initial=bp.qty,
                initial_stop=initial,
                stop=initial,
                entry_date=entry_day,
                days_held=0,
                partial_taken=False,
                signature=plan.signature if plan else "unknown",
                win_prob=plan.win_prob if plan else None,
                sector=plan.sector if plan else self.sector_of.get(symbol, ""),
                stop_order_id=stops[0].id if stops else None,
                pending_exit=None,
            )
        )
        if entry is not None:
            self.alert(
                Level.INFO,
                f":moneybag: Bought {symbol}",
                bullets(
                    [
                        f"Bought {shares(bp.qty)} of {symbol} at {usd(bp.avg_entry_price)} each",
                        f"Total: {usd(bp.qty * bp.avg_entry_price)}",
                        f"Stop-loss is at {usd(initial)}",
                    ]
                ),
            )

    def _book_sells(self, row: PositionRow, qty: int, now: datetime) -> None:
        after = datetime.combine(row.entry_date, time(0), tzinfo=ET)
        sells = [
            o
            for o in self.broker.closed_orders(row.symbol, after)
            if o.side == "sell" and o.filled_qty > 0 and o.filled_avg_price
        ]
        for o in sells:
            if self.journal.once(f"sell_seen:{o.id}"):
                fill_day = (o.filled_at or now).astimezone(ET).date()
                settles = self.calendar.next_session(fill_day).day
                self.journal.add_unsettled(o.filled_qty * o.filled_avg_price, settles)
        proceeds = sum(o.filled_qty * (o.filled_avg_price or 0) for o in sells)
        if qty > 0:
            self.journal.upsert_position(
                replace(row, shares=qty, partial_taken=True, realized_proceeds=proceeds)
            )
            return
        sold = sum(o.filled_qty for o in sells) or row.shares_initial
        last = max(sells, key=lambda o: o.filled_at or now) if sells else None
        exit_day = (last.filled_at or now).astimezone(ET).date() if last else now.date()
        pnl = proceeds - row.entry_price * row.shares_initial
        risk = (row.entry_price - row.initial_stop) * row.shares_initial
        reason = exit_reason(last, row)
        self.journal.record_trade(
            symbol=row.symbol,
            entry_date=row.entry_date,
            exit_date=exit_day,
            entry_price=row.entry_price,
            exit_price=proceeds / sold if sold else 0.0,
            shares=row.shares_initial,
            pnl=pnl,
            r_multiple=pnl / risk if risk > 0 else 0.0,
            exit_reason=reason,
            signature=row.signature,
            win_prob=row.win_prob,
        )
        self.journal.delete_position(row.symbol)
        icon = ":chart_with_upwards_trend:" if pnl >= 0 else ":chart_with_downwards_trend:"
        sold_at = f" at {usd(proceeds / sold)} each" if sells and sold else ""
        self.alert(
            Level.INFO,
            f"{icon} Sold {row.symbol}",
            bullets(
                [
                    f"Sold {shares(row.shares_initial)} of {row.symbol}{sold_at}",
                    f"Bought at {usd(row.entry_price)}",
                    f"{'Profit' if pnl >= 0 else 'Loss'}: {usd(abs(pnl))}",
                    f"Why: {reason_text(reason)}",
                ]
            ),
        )

    # ============================================================= protective stops

    def protect(self, actions: list[ProtectAction], now: datetime) -> None:
        for a in actions:
            if a.sell:
                order = self.broker.sell_market(a.symbol, a.qty, a.cid)
                action = (
                    f"The price ({usd(a.current_price)}) was already below the stop-loss "
                    f"({usd(a.stop)}), so the bot sold {shares(a.qty)} right away."
                )
            else:
                order = self.broker.submit_stop(a.symbol, a.qty, a.stop, a.cid)
                action = f"Placed a stop-loss at {usd(a.stop)} for {shares(a.qty)}."
                if a.row is not None and order is not None and not a.row.stop_order_id:
                    self.journal.upsert_position(replace(a.row, stop_order_id=order.id))
            self.record(order, a.cid, a.symbol, "sell", a.qty, "stop", now, stop_price=a.stop)
            if a.expected and not a.sell:  # overnight fill: stops go in after the close
                self.alert(Level.INFO, f"{a.symbol}: stop-loss placed", action)
            else:
                self.alert(Level.CRITICAL, f"{a.symbol} had no stop-loss", action)

    def change_stop(self, change: StopChange) -> None:
        if change.stop_price is not None:
            self.broker.replace_stop(change.order_id, stop_price=change.stop_price)
            log.info("raised stop %s to %.2f", change.symbol, change.stop_price)
            was = f" from {usd(change.old_price)}" if change.old_price else ""
            cover = f" for your {shares(change.held)}" if change.held else ""
            self.alert(
                Level.INFO,
                f":arrow_up: {change.symbol}: stop-loss moved up",
                f"The stop-loss{cover} moved up{was} to {usd(change.stop_price)}. "
                f"If the price falls to {usd(change.stop_price)}, the bot sells automatically. "
                "A stop-loss only ever moves up, never down.",
            )
        else:
            self.broker.replace_stop(change.order_id, qty=change.qty)
            self.alert(
                Level.INFO,
                f"{change.symbol}: stop-loss resized",
                "Some shares were sold, so the stop-loss now protects the "
                f"{shares(change.qty or 0)} you still hold.",
            )

    # ============================================================= entries

    def verify_cash_only(self) -> bool:
        """Re-apply and read back the cash-only lock; alert and block buys if it fails."""
        if self.broker.verify_cash_only(apply=True):
            return True
        self.alert(
            Level.CRITICAL,
            "Buying paused: couldn't confirm the account is cash-only",
            "The bot couldn't confirm that borrowing money (margin) and short selling are switched "
            "off on the account, so it won't buy anything until it can.",
        )
        return False

    def submit_entry(self, e: EntryOrder, now: datetime) -> BrokerOrder | None:
        """The OTO entry: limit buy with its broker-side stop. None in a dry run."""
        order = self.broker.submit_entry(e.symbol, e.shares, e.limit, e.stop, e.cid)
        self.record(order, e.cid, e.symbol, "buy", e.shares, "limit", now, e.limit, e.stop)
        self.alert(
            Level.INFO,
            f"Buy order placed{' (dry run)' if order is None else ''}: {e.symbol}",
            f"Buying up to {shares(e.shares)} of {e.symbol} at {usd(e.limit)} or less "
            f"(up to {usd(e.shares * e.limit)}). Stop-loss {usd(e.stop)}. "
            f"Estimated win chance {e.win_prob:.0%}.",
        )
        return order

    def cancel_entry(self, o: BrokerOrder, now: datetime) -> None:
        self.broker.cancel(o.id)
        self.journal.record_order(
            o.client_order_id, o.id, o.symbol, "buy", o.qty, "limit", "canceled", now
        )

    # ============================================================= exits

    def exit(self, x: ExitIntent, now: datetime) -> None:
        """Swing exit decided at the close: full (cancel stop, sell all) or partial
        (shrink stop, sell part). A failed sell puts the stop back (rule 6)."""
        row, today = x.row, now.date()
        if x.full:
            if row.stop_order_id:
                self.broker.cancel(row.stop_order_id)
            try:
                order = self.broker.sell_market(row.symbol, x.qty, x.cid)
            except BrokerError as exc:
                restop = self.broker.submit_stop(
                    row.symbol, x.qty, row.stop, f"{today}-{row.symbol}-restop-{now:%H%M}"
                )
                self.alert(
                    Level.CRITICAL,
                    f"Couldn't sell {row.symbol}",
                    f"The broker refused the sell order. Reason given: {exc}. "
                    "The stop-loss is back in place, so the shares are still protected.",
                )
                self.journal.upsert_position(
                    replace(row, stop_order_id=restop.id if restop else None)
                )
                return
            self.record(order, x.cid, row.symbol, "sell", x.qty, "market", now)
            self.journal.upsert_position(replace(row, pending_exit=None, stop_order_id=None))
        else:
            if row.stop_order_id:
                self.broker.replace_stop(row.stop_order_id, qty=x.held - x.qty)
            try:
                order = self.broker.sell_market(row.symbol, x.qty, x.cid)
            except BrokerError as exc:
                if row.stop_order_id:  # never leave shares without a stop
                    self.broker.replace_stop(row.stop_order_id, qty=x.held)
                self.alert(
                    Level.CRITICAL,
                    f"Couldn't sell part of {row.symbol}",
                    f"The broker refused the sell order. Reason given: {exc}. "
                    "The stop-loss still covers all the shares.",
                )
                return
            self.record(order, x.cid, row.symbol, "sell", x.qty, "market", now)
            self.journal.upsert_position(replace(row, pending_exit=None))
        self.alert(
            Level.INFO,
            f"Selling {row.symbol}",
            f"Placed a market order to sell {shares(x.qty)} ({reason_text(x.kind)})."
            + (STOP_SWAPPED if x.full and row.stop_order_id else ""),
        )

    def sell_all(self, x: IntradayExit, now: datetime) -> bool:
        """Cancel the symbol's stops, sell everything at market; re-protect on failure."""
        row, qty, cid = x.row, x.qty, x.cid
        cancelled = 0
        for o in flatten_orders(self.broker.open_orders()):
            stop = o.symbol == row.symbol and o.side == "sell" and o.type in STOP_TYPES
            if stop and o.status != "held":
                self.broker.cancel(o.id)
                cancelled += 1
        try:
            order = self.broker.sell_market(row.symbol, qty, cid)
        except BrokerError as exc:
            restop = self.broker.submit_stop(
                row.symbol, qty, row.stop, f"{now.date()}-{row.symbol}-restop-{now:%H%M}"
            )
            self.journal.upsert_position(replace(row, stop_order_id=restop.id if restop else None))
            self.alert(
                Level.CRITICAL,
                f"Couldn't sell {row.symbol}",
                f"The broker refused the sell order. Reason given: {exc}. "
                "The stop-loss is back in place, so the shares are still protected.",
            )
            return False
        self.record(order, cid, row.symbol, "sell", qty, "market", now)
        self.journal.upsert_position(replace(row, pending_exit=None, stop_order_id=None))
        self.alert(Level.INFO, f"Selling {row.symbol}", f"Placed a market order to sell "
                   f"{shares(qty)} ({reason_text(cid.rsplit('-exit-', 1)[1])})."
                   + (STOP_SWAPPED if cancelled else ""))  # fmt: skip
        return True

    # ============================================================= overnight

    def submit_close_buy(
        self, b: OvernightBuy, n: int, win_prob: float, now: datetime
    ) -> BrokerOrder | None:
        """Overnight entry: DAY limit buy just before the close with its stop attached.
        The attached stop is a DAY order too, so ``protect`` at the EOD job replaces it
        with a GTC stop from the one recorded here. None in a dry run."""
        order = self.broker.submit_close_buy(b.symbol, n, b.limit, b.stop, b.cid)
        self.record(order, b.cid, b.symbol, "buy", n, "limit", now, b.limit, b.stop)
        self.alert(
            Level.INFO,
            f"Overnight buy order placed{' (dry run)' if order is None else ''}: {b.symbol}",
            f"Buying up to {shares(n)} of {b.symbol} just before the close for {usd(b.limit)} "
            f"or less (up to {usd(n * b.limit)}), with a stop-loss at {usd(b.stop)}. "
            f"It sells at tomorrow's open. Estimated win chance about {win_prob:.0%} "
            "(overnight mode skips the win-rate check).",
        )
        return order

    def sell_at_open(self, x: OvernightSell, now: datetime) -> bool:
        """Cancel the position's stops and sell it in the opening auction. Stops only
        trigger in regular hours, so nothing is lost before the open; if the sell is
        refused, the stop is put straight back (rule 6)."""
        row, qty = x.row, x.qty
        cancelled = 0
        for o in flatten_orders(self.broker.open_orders()):
            stop = o.symbol == row.symbol and o.side == "sell" and o.type in STOP_TYPES
            if stop and o.status != "held":
                self.broker.cancel(o.id)
                cancelled += 1
        try:
            order = self.broker.sell_at_open(row.symbol, qty, x.cid)
        except BrokerError as exc:
            restop = self.broker.submit_stop(
                row.symbol, qty, row.stop, f"{now.date()}-{row.symbol}-restop-{now:%H%M}"
            )
            self.journal.upsert_position(replace(row, stop_order_id=restop.id if restop else None))
            self.alert(
                Level.CRITICAL,
                f"Couldn't sell {row.symbol} at the open",
                f"The broker refused the sell order. Reason given: {exc}. "
                "The stop-loss is back in place, so the shares are still protected.",
            )
            return False
        self.record(order, x.cid, row.symbol, "sell", qty, "market_on_open", now)
        self.journal.upsert_position(replace(row, pending_exit=None, stop_order_id=None))
        text = f"Placed an order to sell your {shares(qty)} at the opening price."
        if cancelled:
            text += " The stop-loss was swapped for it: a stop can't protect the opening gap."
        self.alert(Level.INFO, f"Selling {row.symbol} at the open", text)
        return True

    def raise_stop(self, row: PositionRow, new_stop: float, order_id: str | None) -> None:
        """Intraday: the journal's stop first, then the broker's (only ever up)."""
        self.journal.upsert_position(replace(row, stop=new_stop))
        if order_id is not None:
            self.broker.replace_stop(order_id, stop_price=new_stop)
            log.info("raised stop %s to %.2f", row.symbol, new_stop)
            self.alert(
                Level.INFO,
                f":arrow_up: {row.symbol}: stop-loss moved up",
                f"The stop-loss for your {shares(row.shares)} moved up from {usd(row.stop)} to "
                f"{usd(new_stop)}. If the price falls to {usd(new_stop)}, the bot sells "
                "automatically. A stop-loss only ever moves up, never down.",
            )
