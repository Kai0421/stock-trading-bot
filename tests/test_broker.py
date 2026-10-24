# Author: Kai0421
"""Tests for bot.broker: the Alpaca trading wrapper (paper only, OTO entries, dry run)."""

from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopOrderRequest,
)

from bot.broker import Broker, BrokerError
from bot.config import ConfigError, Settings


def order(**kw):
    base = dict(
        id="o1",
        client_order_id="c1",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty="10",
        filled_qty="0",
        filled_avg_price=None,
        type=OrderType.LIMIT,
        order_class=OrderClass.OTO,
        limit_price="100.2",
        stop_price=None,
        status=NS(value="new"),
        legs=None,
        submitted_at=datetime(2026, 10, 5, 13, 45, tzinfo=UTC),
        filled_at=None,
    )
    base.update(kw)
    return NS(**base)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.config = NS(max_margin_multiplier="2", no_shorting=False)
        self.fail = None
        self.asset = NS(asset_class=NS(value="us_equity"), tradable=True, status=NS(value="active"))

    def _rec(self, name, *args):
        self.calls.append((name, *args))
        if self.fail == name:
            raise RuntimeError("api down")

    def get_account(self):
        self._rec("get_account")
        return NS(
            cash="10000",
            non_marginable_buying_power="9000",
            buying_power="20000",
            equity="10500",
            trading_blocked=False,
            account_blocked=False,
            status=NS(value="ACTIVE"),
        )

    def get_clock(self):
        self._rec("get_clock")
        t = datetime(2026, 10, 5, 13, 45, tzinfo=UTC)
        return NS(timestamp=t, is_open=True, next_open=t, next_close=t)

    def get_all_positions(self):
        self._rec("get_all_positions")
        return [
            NS(
                symbol="AAPL",
                qty="10",
                qty_available="0",
                avg_entry_price="100.1",
                current_price="101.5",
                asset_class=NS(value="us_equity"),
            )
        ]

    def get_orders(self, filter=None):
        self._rec("get_orders", filter)
        leg = order(
            id="s1", side=OrderSide.SELL, type=OrderType.STOP, stop_price="96", limit_price=None
        )
        return [order(legs=[leg])]

    def get_order_by_client_id(self, client_id):
        self._rec("get_order_by_client_id", client_id)
        if client_id == "missing":
            raise RuntimeError("order not found")
        return order(client_order_id=client_id)

    def get_account_configurations(self):
        self._rec("get_account_configurations")
        return self.config

    def set_account_configurations(self, cfg):
        self._rec("set_account_configurations", cfg)
        self.config = NS(
            max_margin_multiplier=cfg.max_margin_multiplier, no_shorting=cfg.no_shorting
        )
        return self.config

    def get_asset(self, symbol):
        self._rec("get_asset", symbol)
        return self.asset

    def submit_order(self, order_data):
        self._rec("submit_order", order_data)
        return order(client_order_id=order_data.client_order_id)

    def replace_order_by_id(self, order_id, order_data=None):
        self._rec("replace_order_by_id", order_id, order_data)
        return order(id=order_id)

    def cancel_order_by_id(self, order_id):
        self._rec("cancel_order_by_id", order_id)


@pytest.fixture
def client():
    return FakeClient()


def live(client):
    return Broker(client, dry_run=False)


# ------------------------------------------------------- reads


def test_account_uses_cash_fields_not_buying_power(client):
    snap = live(client).account()
    assert snap.account.cash == 10_000
    assert snap.account.non_marginable_buying_power == 9_000
    assert snap.account.equity == 10_500
    assert not snap.blocked


def test_account_error_becomes_broker_error(client):
    client.fail = "get_account"
    with pytest.raises(BrokerError):
        live(client).account()


def test_positions_and_orders_are_normalised(client):
    b = live(client)
    p = b.positions()["AAPL"]
    assert (p.qty, p.qty_available, p.avg_entry_price) == (10, 0, 100.1)
    (o,) = b.open_orders()
    assert o.side == "buy" and o.type == "limit" and o.order_class == "oto"
    assert o.legs[0].side == "sell" and o.legs[0].stop_price == 96.0
    req = client.calls[-1][1]
    assert isinstance(req, GetOrdersRequest)
    assert req.status == QueryOrderStatus.OPEN and req.nested is True


def test_order_by_client_id_missing_is_none(client):
    assert live(client).order_by_client_id("missing") is None
    assert live(client).order_by_client_id("c9").client_order_id == "c9"


def test_clock(client):
    assert live(client).clock().is_open


# ------------------------------------------------------- cash-only lock


def test_cash_only_lock_applied_and_verified(client):
    assert live(client).verify_cash_only(apply=True)
    cfg = next(c[1] for c in client.calls if c[0] == "set_account_configurations")
    assert cfg.max_margin_multiplier == "1" and cfg.no_shorting is True


def test_cash_only_lock_mismatch_detected(client):
    assert not live(client).verify_cash_only(apply=False)


def test_cash_only_dry_run_reads_only(client):
    client.config = NS(max_margin_multiplier="1", no_shorting=True)
    assert Broker(client, dry_run=True).verify_cash_only(apply=True)
    assert not any(c[0] == "set_account_configurations" for c in client.calls)


def test_cash_only_read_error_fails_closed(client):
    client.fail = "get_account_configurations"
    assert not live(client).verify_cash_only(apply=False)


# ------------------------------------------------------- assets


def test_asset_checks(client):
    b = live(client)
    assert b.check_asset("AAPL") is None
    client.asset = NS(asset_class=NS(value="crypto"), tradable=True, status=NS(value="active"))
    assert "asset class" in b.check_asset("BTCUSD")
    client.asset = NS(asset_class=NS(value="us_equity"), tradable=False, status=NS(value="active"))
    assert "not tradable" in b.check_asset("XYZ")
    client.fail = "get_asset"
    assert b.check_asset("AAPL") is not None


# ------------------------------------------------------- orders


def test_entry_is_oto_gtc_limit_with_stop(client):
    o = live(client).submit_entry("AAPL", 10, 100.2, 96.0, "2026-10-05-AAPL-entry")
    req = client.calls[-1][1]
    assert isinstance(req, LimitOrderRequest)
    assert req.side == OrderSide.BUY
    assert req.order_class == OrderClass.OTO
    assert req.time_in_force == TimeInForce.GTC  # a DAY stop would expire overnight
    assert req.limit_price == 100.2
    assert req.stop_loss.stop_price == 96.0
    assert req.qty == 10
    assert req.client_order_id == "2026-10-05-AAPL-entry"
    assert o.client_order_id == "2026-10-05-AAPL-entry"


def test_close_buy_is_day_oto_limit_with_stop(client):
    """Overnight entry: a plain DAY limit with its stop attached, not a closing-auction
    order (Alpaca paper let limit-on-close buys expire below their limit, 2026-10-09)."""
    live(client).submit_close_buy("NVDA", 21, 231.8, 220.21, "2026-10-09-NVDA-entry-close")
    req = client.calls[-1][1]
    assert isinstance(req, LimitOrderRequest)
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.DAY  # unfilled by the close: it expires
    assert req.order_class == OrderClass.OTO
    assert req.stop_loss.stop_price == 220.21
    assert req.limit_price == 231.8 and req.qty == 21
    assert req.client_order_id == "2026-10-09-NVDA-entry-close"


@pytest.mark.parametrize(
    ("qty", "limit", "stop"),
    [(0, 100.0, 96.0), (10, 100.0, 100.0), (10, 100.0, 0.0), (1.5, 100.0, 96.0)],
)
def test_close_buy_guards(client, qty, limit, stop):
    with pytest.raises(BrokerError):
        live(client).submit_close_buy("NVDA", qty, limit, stop, "cid")
    assert not any(c[0] == "submit_order" for c in client.calls)


@pytest.mark.parametrize(
    ("qty", "limit", "stop"),
    [(0, 100.0, 96.0), (10, 100.0, 100.0), (10, 100.0, 0.0), (1.5, 100.0, 96.0)],
)
def test_entry_guards(client, qty, limit, stop):
    with pytest.raises(BrokerError):
        live(client).submit_entry("AAPL", qty, limit, stop, "cid")
    assert not any(c[0] == "submit_order" for c in client.calls)


def test_sell_and_stop_orders(client):
    b = live(client)
    b.sell_market("AAPL", 5, "cid-sell")
    req = client.calls[-1][1]
    assert isinstance(req, MarketOrderRequest)
    assert req.side == OrderSide.SELL and req.time_in_force == TimeInForce.DAY and req.qty == 5
    b.submit_stop("AAPL", 5, 96.0, "cid-stop")
    req = client.calls[-1][1]
    assert isinstance(req, StopOrderRequest)
    assert req.side == OrderSide.SELL and req.time_in_force == TimeInForce.GTC
    assert req.stop_price == 96.0
    with pytest.raises(BrokerError):
        b.sell_market("AAPL", 0, "x")


def test_replace_and_cancel(client):
    b = live(client)
    b.replace_stop("s1", stop_price=100.0)
    name, oid, req = client.calls[-1]
    assert (name, oid) == ("replace_order_by_id", "s1")
    assert isinstance(req, ReplaceOrderRequest) and req.stop_price == 100.0 and req.qty is None
    b.replace_stop("s1", qty=5)
    assert client.calls[-1][2].qty == 5
    b.cancel("o1")
    assert client.calls[-1] == ("cancel_order_by_id", "o1")


def test_dry_run_sends_nothing(client):
    b = Broker(client, dry_run=True)
    assert b.submit_entry("AAPL", 10, 100.2, 96.0, "cid") is None
    assert b.sell_market("AAPL", 5, "cid") is None
    assert b.submit_stop("AAPL", 5, 96.0, "cid") is None
    b.replace_stop("s1", stop_price=100.0)
    b.cancel("o1")
    mutating = {"submit_order", "replace_order_by_id", "cancel_order_by_id"}
    assert not any(c[0] in mutating for c in client.calls)


def test_order_errors_become_broker_errors(client):
    client.fail = "submit_order"
    with pytest.raises(BrokerError):
        live(client).submit_entry("AAPL", 10, 100.2, 96.0, "cid")


def test_from_settings_is_paper_only(monkeypatch):
    with pytest.raises(ConfigError):
        Broker.from_settings(Settings())  # no keys
    seen = {}

    class TC:
        def __init__(self, key, secret, paper):
            seen.update(key=key, paper=paper)

    monkeypatch.setattr("bot.broker._trading_client_class", lambda: TC)
    b = Broker.from_settings(Settings(alpaca_api_key="k", alpaca_secret_key="s", dry_run=False))
    assert seen == {"key": "k", "paper": True}
    assert b.dry_run is False
