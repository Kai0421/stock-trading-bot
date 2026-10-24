# Author: Kai0421
"""Alpaca trading wrapper: paper only, cash only, deterministic order requests.

- The client is always created with ``paper=True``; ``Settings`` refuses ``PAPER=false``.
- Entries are **OTO** orders: a GTC limit buy with an attached GTC stop-loss, so the
  stop exists at the broker from the moment of fill (docs/01 rule 6). Alpaca's
  ``bracket`` class would also force a take-profit, which the strategy doesn't have,
  and a DAY stop would expire overnight. Unfilled entries are cancelled by the engine.
- ``dry_run`` logs every mutating call and sends nothing.
- Hard-rule guards are repeated here as a last line of defence: whole shares only,
  0 < stop < limit, sells never placed for zero shares. ``buying_power`` is never read.
- Every API error becomes ``BrokerError`` so jobs fail closed.
"""

from __future__ import annotations

import logging
import types
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bot.config import ConfigError, Settings
from bot.risk import ALLOWED_ASSET_CLASSES, Account

log = logging.getLogger(__name__)
MAX_CLIENT_ORDER_ID = 128


class BrokerError(RuntimeError):
    pass


def _trading_client_class() -> Any:
    from alpaca.trading.client import TradingClient

    return TradingClient


def _val(x: Any) -> Any:
    """Enum -> its value, lower-cased when it is a string."""
    v = getattr(x, "value", x)
    return v.lower() if isinstance(v, str) else v


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


@dataclass(frozen=True)
class AccountSnapshot:
    account: Account
    trading_blocked: bool
    account_blocked: bool
    status: str

    @property
    def blocked(self) -> bool:
        return self.trading_blocked or self.account_blocked or self.status != "active"


@dataclass(frozen=True)
class ClockInfo:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int
    qty_available: int
    avg_entry_price: float
    current_price: float
    asset_class: str


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    type: str
    order_class: str
    limit_price: float | None
    stop_price: float | None
    status: str
    legs: tuple[BrokerOrder, ...]
    submitted_at: datetime | None
    filled_at: datetime | None

    @classmethod
    def from_alpaca(cls, o: Any) -> BrokerOrder:
        return cls(
            id=str(o.id),
            client_order_id=str(o.client_order_id),
            symbol=o.symbol,
            side=_val(o.side),
            qty=float(o.qty or 0),
            filled_qty=float(o.filled_qty or 0),
            filled_avg_price=_f(o.filled_avg_price),
            type=_val(o.type),
            order_class=_val(o.order_class) or "simple",
            limit_price=_f(o.limit_price),
            stop_price=_f(o.stop_price),
            status=_val(o.status),
            legs=tuple(cls.from_alpaca(leg) for leg in (o.legs or ())),
            submitted_at=o.submitted_at,
            filled_at=o.filled_at,
        )


class Broker:
    def __init__(self, client: Any, dry_run: bool) -> None:
        self._c = client
        self.dry_run = dry_run

    @classmethod
    def from_settings(cls, settings: Settings) -> Broker:
        if not (settings.alpaca_api_key and settings.alpaca_secret_key):
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for trading")
        if not settings.paper:  # Settings already refuses this; belt and braces
            raise ConfigError("paper trading only")
        client = _trading_client_class()(
            settings.alpaca_api_key, settings.alpaca_secret_key, paper=True
        )
        return cls(client, dry_run=settings.dry_run)

    def _call(self, what: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as exc:
            raise BrokerError(f"{what} failed: {exc}") from exc

    # ----------------------------------------------------------------- reads

    def account(self) -> AccountSnapshot:
        a = self._call("get_account", self._c.get_account)
        try:
            account = Account(
                cash=float(a.cash),
                non_marginable_buying_power=float(a.non_marginable_buying_power),
                equity=float(a.equity),
            )
        except (TypeError, ValueError) as exc:
            raise BrokerError(f"unreadable account balances: {exc}") from exc
        return AccountSnapshot(
            account=account,
            trading_blocked=bool(a.trading_blocked),
            account_blocked=bool(a.account_blocked),
            status=_val(a.status),
        )

    def clock(self) -> ClockInfo:
        c = self._call("get_clock", self._c.get_clock)
        return ClockInfo(c.timestamp, bool(c.is_open), c.next_open, c.next_close)

    def positions(self) -> dict[str, BrokerPosition]:
        out = {}
        for p in self._call("get_all_positions", self._c.get_all_positions):
            out[p.symbol] = BrokerPosition(
                symbol=p.symbol,
                qty=int(float(p.qty)),
                qty_available=int(float(p.qty_available or 0)),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price or 0),
                asset_class=_val(p.asset_class),
            )
        return out

    def _orders(self, **filters: Any) -> list[BrokerOrder]:
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(nested=True, **filters)
        return [
            BrokerOrder.from_alpaca(o) for o in self._call("get_orders", self._c.get_orders, req)
        ]

    def open_orders(self) -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus

        return self._orders(status=QueryOrderStatus.OPEN, limit=500)

    def closed_orders(self, symbol: str, after: datetime) -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus

        return self._orders(
            status=QueryOrderStatus.CLOSED, symbols=[symbol], after=after, limit=500
        )

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            return BrokerOrder.from_alpaca(self._c.get_order_by_client_id(client_order_id))
        except Exception:
            return None

    def check_asset(self, symbol: str) -> str | None:
        """None if Alpaca says the asset is an active, tradable US equity."""
        try:
            a = self._c.get_asset(symbol)
        except Exception as exc:
            return f"{symbol}: asset lookup failed ({exc})"
        if _val(a.asset_class) not in ALLOWED_ASSET_CLASSES:
            return f"{symbol}: asset class {_val(a.asset_class)!r} not allowed"
        if not a.tradable:
            return f"{symbol}: not tradable"
        if _val(a.status) != "active":
            return f"{symbol}: asset status {_val(a.status)}"
        return None

    # ----------------------------------------------------------------- cash-only lock

    def verify_cash_only(self, apply: bool) -> bool:
        """Set (unless dry run) and read back max_margin_multiplier=1, no_shorting=true."""
        try:
            cfg = self._c.get_account_configurations()
            locked = str(cfg.max_margin_multiplier) == "1" and cfg.no_shorting is True
            if locked or not apply:
                return locked
            if self.dry_run:
                log.info("DRY RUN: would set max_margin_multiplier=1, no_shorting=true")
                return False
            update = {"max_margin_multiplier": "1", "no_shorting": True}
            new = (
                cfg.model_copy(update=update)
                if hasattr(cfg, "model_copy")
                else types.SimpleNamespace(**{**vars(cfg), **update})
            )
            self._c.set_account_configurations(new)
            back = self._c.get_account_configurations()
            return str(back.max_margin_multiplier) == "1" and back.no_shorting is True
        except Exception as exc:
            log.error("cash-only lock check failed: %s", exc)
            return False

    # ----------------------------------------------------------------- orders

    def _submit(self, what: str, request: Any) -> BrokerOrder | None:
        if len(request.client_order_id or "") > MAX_CLIENT_ORDER_ID:
            raise BrokerError("client_order_id too long")
        if self.dry_run:
            log.info("DRY RUN: would submit %s %s", what, request.model_dump(exclude_none=True))
            return None
        return BrokerOrder.from_alpaca(self._call(what, self._c.submit_order, request))

    def submit_entry(
        self, symbol: str, qty: int, limit_price: float, stop_price: float, client_order_id: str
    ) -> BrokerOrder | None:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest

        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise BrokerError(f"entry qty must be a positive whole number (got {qty})")
        if not 0 < stop_price < limit_price:
            raise BrokerError(f"need 0 < stop < limit (stop={stop_price}, limit={limit_price})")
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=limit_price,
            order_class=OrderClass.OTO,
            stop_loss=StopLossRequest(stop_price=stop_price),
            client_order_id=client_order_id,
        )
        return self._submit("entry", req)

    def submit_stop(
        self, symbol: str, qty: int, stop_price: float, client_order_id: str
    ) -> BrokerOrder | None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        if qty <= 0 or stop_price <= 0:
            raise BrokerError("stop needs qty > 0 and stop price > 0")
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price,
            client_order_id=client_order_id,
        )
        return self._submit("stop", req)

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrder | None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if qty <= 0:
            raise BrokerError(f"sell qty must be positive (got {qty})")
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        return self._submit("sell", req)

    def submit_close_buy(
        self, symbol: str, qty: int, limit_price: float, stop_price: float, client_order_id: str
    ) -> BrokerOrder | None:
        """Overnight entry, sent a few minutes before the close: a DAY limit buy with its
        stop attached (OTO), so the position is protected from the fill. Unfilled by the
        close, it expires. Not a closing-auction (CLS) order: Alpaca paper let those
        expire unfilled below their limit (2026-10-09)."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest

        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise BrokerError(f"entry qty must be a positive whole number (got {qty})")
        if not 0 < stop_price < limit_price:
            raise BrokerError(f"need 0 < stop < limit (stop={stop_price}, limit={limit_price})")
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            order_class=OrderClass.OTO,
            stop_loss=StopLossRequest(stop_price=stop_price),
            client_order_id=client_order_id,
        )
        return self._submit("close buy", req)

    def sell_at_open(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrder | None:
        """Market-on-open sell (overnight mode): fills in the opening auction. Alpaca
        takes these from 19:00 the evening before until 09:28 ET."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if qty <= 0:
            raise BrokerError(f"sell qty must be positive (got {qty})")
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.OPG,
            client_order_id=client_order_id,
        )
        return self._submit("open sell", req)

    def replace_stop(
        self, order_id: str, stop_price: float | None = None, qty: int | None = None
    ) -> BrokerOrder | None:
        from alpaca.trading.requests import ReplaceOrderRequest

        req = ReplaceOrderRequest(stop_price=stop_price, qty=qty)
        if self.dry_run:
            log.info(
                "DRY RUN: would replace %s with %s", order_id, req.model_dump(exclude_none=True)
            )
            return None
        return BrokerOrder.from_alpaca(
            self._call("replace_order", self._c.replace_order_by_id, order_id, req)
        )

    def cancel(self, order_id: str) -> None:
        if self.dry_run:
            log.info("DRY RUN: would cancel %s", order_id)
            return
        self._call("cancel_order", self._c.cancel_order_by_id, order_id)
