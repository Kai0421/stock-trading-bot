# Author: Kai0421
"""Tests for bot.data (daily bars via Alpaca, cleaned and cached) and bot.universe."""

from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrameUnit

from bot.config import ConfigError, Settings
from bot.data import (
    BarCache,
    bars_request,
    barset_to_frames,
    clean_bars,
    load_daily_bars,
    make_data_client,
)
from bot.risk import LEVERAGED_OR_INVERSE
from bot.universe import BENCHMARK, UNIVERSE, sector_of, symbols

ET = ZoneInfo("America/New_York")


def raw_bar(day, o=100.0, h=101.0, lo=99.0, c=100.5, v=1_000_000):
    return {"t": f"{day}T04:00:00Z", "o": o, "h": h, "l": lo, "c": c, "v": v, "n": 10, "vw": c}


def barset(data):
    return BarSet(raw_data=data)


def frame(days, start_price=100.0):
    idx = pd.DatetimeIndex(pd.to_datetime(days)).as_unit("ns")
    c = np.linspace(start_price, start_price + len(days) - 1, len(days))
    return pd.DataFrame(
        {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1e6}, index=idx
    )


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.requests = []

    def get_stock_bars(self, request_params):
        self.requests.append(request_params)
        syms = request_params.symbol_or_symbols
        syms = [syms] if isinstance(syms, str) else syms
        return barset({s: self.data[s] for s in syms if s in self.data})


DAYS = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]


# ------------------------------------------------------- request / parsing


def test_bars_request_is_real_alpaca_request():
    req = bars_request(["AAPL", "MSFT"], date(2016, 1, 1), date(2026, 9, 30))
    assert isinstance(req, StockBarsRequest)
    assert req.symbol_or_symbols == ["AAPL", "MSFT"]
    assert req.timeframe.amount_value == 1
    assert req.timeframe.unit_value == TimeFrameUnit.Day
    assert req.adjustment == Adjustment.ALL
    assert req.feed == DataFeed.SIP
    # alpaca-py stores request times as naive UTC: 00:00 ET -> 05:00 UTC (EST).
    assert req.start == datetime(2016, 1, 1, 5, 0)
    assert req.end == datetime(2026, 10, 1, 3, 59)  # 23:59 EDT


def test_bars_request_iex_feed_and_bad_range():
    assert bars_request(["SPY"], date(2020, 1, 1), date(2020, 2, 1), feed="iex").feed == (
        DataFeed.IEX
    )
    with pytest.raises(ValueError):
        bars_request(["SPY"], date(2020, 2, 1), date(2020, 1, 1))


def test_barset_to_frames_uses_et_dates():
    bs = barset({"AAPL": [raw_bar(d) for d in DAYS[:2]], "SPY": [raw_bar(DAYS[0])]})
    frames = barset_to_frames(bs)
    assert set(frames) == {"AAPL", "SPY"}
    aapl = frames["AAPL"]
    assert list(aapl.columns) == ["open", "high", "low", "close", "volume"]
    assert list(aapl.index) == [pd.Timestamp("2026-09-28"), pd.Timestamp("2026-09-29")]
    assert aapl.index.tz is None
    assert aapl["close"].iloc[0] == 100.5


def test_barset_to_frames_empty():
    assert barset_to_frames(barset({})) == {}


# ------------------------------------------------------- cleaning


def test_clean_bars_sorts():
    df = frame(DAYS).iloc[::-1]
    assert clean_bars(df, "X").index.is_monotonic_increasing


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d.assign(close=np.nan),
        lambda d: d.assign(low=d["high"] + 1),
        lambda d: d.assign(open=0.0),
        lambda d: d.assign(volume=-1.0),
        lambda d: pd.concat([d, d.iloc[-1:]]),
        lambda d: d.drop(columns="volume"),
    ],
)
def test_clean_bars_rejects_bad_data(corrupt):
    with pytest.raises(ValueError):
        clean_bars(corrupt(frame(DAYS)), "X")


# ------------------------------------------------------- cache


def test_cache_roundtrip_and_coverage(tmp_path):
    cache = BarCache(tmp_path)
    df = frame(DAYS)
    cache.write("AAPL", df, date(2026, 9, 28), date(2026, 10, 2))
    pd.testing.assert_frame_equal(cache.read("AAPL"), df, check_freq=False)
    assert cache.covers("AAPL", date(2026, 9, 29), date(2026, 10, 1))
    assert not cache.covers("AAPL", date(2026, 9, 1), date(2026, 10, 1))
    assert not cache.covers("MSFT", date(2026, 9, 29), date(2026, 10, 1))
    assert cache.read("MSFT") is None


def test_cache_handles_class_share_symbols(tmp_path):
    cache = BarCache(tmp_path)
    cache.write("BRK.B", frame(DAYS), date(2026, 9, 28), date(2026, 10, 2))
    assert cache.read("BRK.B") is not None


# ------------------------------------------------------- loading


def test_load_fetches_then_uses_cache(tmp_path):
    client = FakeClient({"AAPL": [raw_bar(d) for d in DAYS], "SPY": [raw_bar(d) for d in DAYS]})
    cache = BarCache(tmp_path)
    start, end = date(2026, 9, 28), date(2026, 10, 2)
    first = load_daily_bars(client, ["AAPL", "SPY"], start, end, cache)
    assert set(first.bars) == {"AAPL", "SPY"}
    assert first.skipped == {}
    assert len(client.requests) == 1
    second = load_daily_bars(client, ["AAPL", "SPY"], start, end, cache)
    assert len(client.requests) == 1  # served from cache
    pd.testing.assert_frame_equal(second.bars["AAPL"], first.bars["AAPL"], check_freq=False)


def test_load_trims_to_requested_range(tmp_path):
    client = FakeClient({"AAPL": [raw_bar(d) for d in DAYS]})
    res = load_daily_bars(
        client, ["AAPL"], date(2026, 9, 29), date(2026, 10, 1), BarCache(tmp_path)
    )
    assert len(res.bars["AAPL"]) == 3


def test_load_skips_missing_and_bad_symbols(tmp_path):
    bad = [raw_bar(DAYS[0]), raw_bar(DAYS[1], h=90.0, lo=99.0)]
    client = FakeClient({"AAPL": [raw_bar(d) for d in DAYS], "BAD": bad})
    res = load_daily_bars(
        client, ["AAPL", "BAD", "GONE"], date(2026, 9, 28), date(2026, 10, 2), BarCache(tmp_path)
    )
    assert set(res.bars) == {"AAPL"}
    assert set(res.skipped) == {"BAD", "GONE"}
    assert BarCache(tmp_path).read("BAD") is None


def test_load_batches_requests(tmp_path):
    data = {f"S{i}": [raw_bar(d) for d in DAYS] for i in range(5)}
    client = FakeClient(data)
    load_daily_bars(
        client, list(data), date(2026, 9, 28), date(2026, 10, 2), BarCache(tmp_path), batch=2
    )
    assert [len(r.symbol_or_symbols) for r in client.requests] == [2, 2, 1]


def test_make_data_client_needs_keys():
    with pytest.raises(ConfigError):
        make_data_client(Settings())
    client = make_data_client(Settings(alpaca_api_key="k", alpaca_secret_key="s"))
    assert hasattr(client, "get_stock_bars")


# ------------------------------------------------------- universe


def test_universe_has_benchmark_and_sectors():
    assert BENCHMARK == "SPY"
    assert "SPY" in UNIVERSE
    assert len(symbols()) >= 100
    assert all(sector for sector in UNIVERSE.values())
    assert sector_of("AAPL") == "Information Technology"
    assert sector_of("XLK") == "Information Technology"  # sector ETFs share the sector limit


def test_universe_excludes_leveraged_products():
    assert not set(UNIVERSE) & LEVERAGED_OR_INVERSE


def test_unknown_symbol_has_no_sector():
    assert sector_of("ZZZZ") == ""


# ------------------------------------------------------- latest prices


def test_latest_prices_uses_real_request_and_feed():
    from alpaca.data.requests import StockLatestTradeRequest

    from bot.data import latest_prices

    class C:
        def __init__(self):
            self.req = None

        def get_stock_latest_trade(self, req):
            self.req = req
            return {"AAPL": NS(price=101.25), "MSFT": NS(price=0.0)}

    c = C()
    prices = latest_prices(c, ["AAPL", "MSFT", "GONE"], feed="iex")
    assert isinstance(c.req, StockLatestTradeRequest)
    assert c.req.feed == DataFeed.IEX
    assert prices == {"AAPL": 101.25}  # zero / missing prices are dropped


def test_latest_prices_error_returns_empty():
    from bot.data import latest_prices

    class C:
        def get_stock_latest_trade(self, req):
            raise RuntimeError("down")

    assert latest_prices(C(), ["AAPL"]) == {}


def test_bars_request_accepts_end_datetime():
    end = datetime(2026, 10, 5, 16, 0, tzinfo=ET)
    req = bars_request(["SPY"], date(2026, 9, 1), end)
    assert req.end == datetime(2026, 10, 5, 20, 0)  # 16:00 EDT as naive UTC


def test_load_without_cache_always_fetches():
    client = FakeClient({"AAPL": [raw_bar(d) for d in DAYS]})
    for _ in range(2):
        res = load_daily_bars(client, ["AAPL"], date(2026, 9, 28), date(2026, 10, 2), cache=None)
        assert len(res.bars["AAPL"]) == 5
    assert len(client.requests) == 2
