# Author: Kai0421
"""Tests for bot.altdata: EDGAR Form 4 / 13D, news veto, point-in-time features, provider."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from bot.altdata import AltDataProvider, compute_features, make_provider
from bot.altdata.edgar import EdgarClient, EdgarError, parse_form4
from bot.altdata.news import fetch_news, is_major
from bot.altdata.store import AltStore, InsiderTxn
from bot.backtest import apply_altdata
from bot.config import ConfigError, Settings
from bot.journal import Journal

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
 <aff10b5One>{plan}</aff10b5One>
 <issuer><issuerCik>0000320193</issuerCik><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
 <reportingOwner>
  <reportingOwnerId><rptOwnerCik>1</rptOwnerCik><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><isDirector>{director}</isDirector><isOfficer>{officer}</isOfficer>
   <officerTitle>{title}</officerTitle></reportingOwnerRelationship>
 </reportingOwner>
 <nonDerivativeTable>
  <nonDerivativeTransaction>
   <transactionDate><value>2026-09-28</value></transactionDate>
   <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
   <transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
    <transactionPricePerShare><value>{price}</value></transactionPricePerShare></transactionAmounts>
  </nonDerivativeTransaction>
 </nonDerivativeTable>
 <derivativeTable><derivativeTransaction>
   <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
 </derivativeTransaction></derivativeTable>
</ownershipDocument>"""


def form4(name="DOE JANE", director="0", officer="1", title="Chief Executive Officer",
          code="P", shares="1000", price="200", plan="0"):  # fmt: skip
    return FORM4.format(name=name, director=director, officer=officer, title=title, code=code,
                        shares=shares, price=price, plan=plan)  # fmt: skip


# ------------------------------------------------------- Form 4 parsing


def test_parse_form4_open_market_purchase():
    owner, txns = parse_form4(form4())
    assert owner.name == "DOE JANE" and owner.is_officer and not owner.is_director
    assert owner.title == "Chief Executive Officer"
    (t,) = txns  # derivative (options) rows are ignored
    assert (t.code, t.shares, t.price, t.plan_10b5_1) == ("P", 1000.0, 200.0, False)
    assert t.tx_date == date(2026, 9, 28)


def test_parse_form4_flags_and_bad_xml():
    owner, txns = parse_form4(form4(director="true", officer="0", title="", code="S", plan="1"))
    assert owner.is_director and not owner.is_officer
    assert txns[0].plan_10b5_1
    with pytest.raises(EdgarError):
        parse_form4("<not-xml")


# ------------------------------------------------------- EDGAR client


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        body, status = self.routes.get(url, ("", 404))
        return NS(status_code=status, text=body, json=lambda b=body: json.loads(b))


SUBMISSIONS = {
    "filings": {
        "recent": {
            "accessionNumber": ["0001-26-000003", "0001-26-000002", "0001-26-000001"],
            "form": ["4", "SC 13D", "10-K"],
            "filingDate": ["2026-10-01", "2026-09-20", "2026-09-01"],
            "acceptanceDateTime": [
                "2026-10-01T17:00:00.000Z",
                "2026-09-20T12:00:00.000Z",
                "2026-09-01T12:00:00.000Z",
            ],
            "primaryDocument": ["xslF345X05/wk-form4.xml", "sc13d.htm", "10k.htm"],
        },
        "files": [],
    }
}


def edgar(routes, sleeps=None):
    http = FakeHttp(routes)
    clock = iter(float(i) * 0.01 for i in range(10_000))
    return (
        EdgarClient(
            "Bot me@example.com",
            get=http,
            sleep=(sleeps.append if sleeps is not None else lambda s: None),
            monotonic=lambda: next(clock),
        ),
        http,
    )


def test_edgar_requires_user_agent():
    with pytest.raises(ConfigError):
        EdgarClient("")


def test_edgar_ticker_map_and_filings():
    routes = {
        "https://www.sec.gov/files/company_tickers.json": (
            json.dumps(
                {
                    "0": {"cik_str": 320193, "ticker": "AAPL"},
                    "1": {"cik_str": 1067983, "ticker": "BRK-B"},
                }
            ),
            200,
        ),
        "https://data.sec.gov/submissions/CIK0000320193.json": (json.dumps(SUBMISSIONS), 200),
    }
    client, http = edgar(routes)
    ciks = client.ticker_ciks()
    assert ciks["AAPL"] == 320193 and ciks["BRK.B"] == 1067983  # Alpaca uses '.'
    filings = client.filings(320193, {"4", "SC 13D"}, since=date(2026, 9, 1))
    assert [f.form for f in filings] == ["4", "SC 13D"]
    assert filings[0].filing_date == date(2026, 10, 1)
    assert filings[0].raw_document == "wk-form4.xml"  # xsl rendering prefix stripped
    assert all(h["User-Agent"] == "Bot me@example.com" for _, h in http.calls)


def test_edgar_rate_limit_sleeps():
    sleeps = []
    routes = {"https://data.sec.gov/submissions/CIK0000320193.json": (json.dumps(SUBMISSIONS), 200)}
    client, http = edgar(routes, sleeps)
    for _ in range(3):
        client.filings(320193, {"4"}, since=date(2026, 1, 1))
    assert len(http.calls) == 3
    assert sleeps and all(s <= 0.2 for s in sleeps)  # <= 5 requests per second


def test_edgar_http_error():
    client, _ = edgar({})
    with pytest.raises(EdgarError):
        client.ticker_ciks()


# ------------------------------------------------------- news


def test_major_news_keywords():
    assert is_major("Apple to acquire Foo for $2B")
    assert is_major("FDA places clinical hold on trial")
    assert is_major("Company cuts guidance for 2026")
    assert not is_major("Apple shares rise after product event")


def test_fetch_news_paginates():
    from alpaca.data.requests import NewsRequest

    pages = [
        {"news": [NS(id=1, headline="a", created_at=datetime(2026, 10, 1, tzinfo=UTC))],
         "next_page_token": "p2"},
        {"news": [NS(id=2, headline="b", created_at=datetime(2026, 10, 2, tzinfo=UTC))],
         "next_page_token": None},
    ]  # fmt: skip
    reqs = []

    class C:
        def get_news(self, req):
            reqs.append(req)
            return NS(data={"news": pages[len(reqs) - 1]["news"]},
                      next_page_token=pages[len(reqs) - 1]["next_page_token"])  # fmt: skip

    items = fetch_news(
        C(), "AAPL", datetime(2026, 9, 30, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC)
    )
    assert [i[0] for i in items] == [1, 2]
    assert isinstance(reqs[0], NewsRequest) and reqs[1].page_token == "p2"


# ------------------------------------------------------- features (point in time)


@pytest.fixture
def store(tmp_path):
    return AltStore(Journal(tmp_path / "bot.db"))


def txn(acc, owner, code="P", shares=1000, price=200, filed=date(2026, 9, 29), title="",
        officer=True, director=False, plan=False):  # fmt: skip
    return InsiderTxn(acc, 0, "AAPL", owner, officer, director, title, code, shares, price,
                      date(2026, 9, 28), filed, plan)  # fmt: skip


def test_cluster_buy_needs_two_insiders_and_250k(store):
    store.add_txns([txn("a1", "X", shares=1000, price=100)])
    assert not compute_features(store, "AAPL", date(2026, 10, 5))["insider_cluster_buy_90d"]
    store.add_txns([txn("a2", "Y", shares=1000, price=200, director=True, officer=False)])
    assert compute_features(store, "AAPL", date(2026, 10, 5))["insider_cluster_buy_90d"]


def test_features_only_use_filings_before_as_of(store):
    store.add_txns([txn("a1", "X", filed=date(2026, 10, 5), title="CEO")])
    assert not compute_features(store, "AAPL", date(2026, 10, 5))["insider_ceo_cfo_buy_30d"]
    assert compute_features(store, "AAPL", date(2026, 10, 6))["insider_ceo_cfo_buy_30d"]


def test_ceo_cfo_and_heavy_selling(store):
    store.add_txns([txn("b1", "CFO GUY", title="Chief Financial Officer")])
    sells = [txn(f"s{i}", f"O{i}", code="S") for i in range(3)]
    planned = [txn(f"p{i}", f"Q{i}", code="S", plan=True) for i in range(5)]
    store.add_txns(sells + planned)
    f = compute_features(store, "AAPL", date(2026, 10, 5))
    assert f["insider_ceo_cfo_buy_30d"]
    assert f["insider_heavy_selling_30d"]
    store2_sells = [txn(f"t{i}", f"R{i}", code="S", plan=True) for i in range(5)]
    assert all(t.plan_10b5_1 for t in store2_sells)


def test_activist_and_news_veto(store):
    store.add_filing("f1", "AAPL", "SC 13D", date(2026, 9, 1))
    store.add_news(7, "AAPL", datetime(2026, 10, 2, 15, tzinfo=UTC), "Apple to acquire Foo")
    f = compute_features(store, "AAPL", date(2026, 10, 5))
    assert f["activist_13d_60d"]
    assert f["news_shock_veto"]  # Friday news is within the last 2 trading days of Monday
    assert not compute_features(store, "AAPL", date(2026, 10, 8))["news_shock_veto"]
    assert not compute_features(store, "AAPL", date(2026, 11, 5))["activist_13d_60d"]


# ------------------------------------------------------- provider


def test_provider_fails_closed_on_errors(store):
    class Broken:
        def refresh(self, *a):
            raise RuntimeError("SEC down")

    p = AltDataProvider(store, Broken(), frozenset({"news_shock_veto"}))
    f = p("AAPL", date(2026, 10, 5))
    assert f["altdata_error"] and f["news_shock_veto"] and f["insider_heavy_selling_30d"]
    assert not f.get("insider_cluster_buy_90d")


def test_provider_refreshes_once_per_day(store):
    calls = []

    class Src:
        def refresh(self, symbol, as_of):
            calls.append((symbol, as_of))

    p = AltDataProvider(store, Src(), frozenset({"news_shock_veto"}))
    p("AAPL", date(2026, 10, 5))
    p("AAPL", date(2026, 10, 5))
    assert calls == [("AAPL", date(2026, 10, 5))]


def test_make_provider(tmp_path):
    j = Journal(tmp_path / "bot.db")
    assert make_provider(Settings(), j, None) is None
    with pytest.raises(ConfigError):
        make_provider(Settings(altdata_features=frozenset({"insider_cluster_buy_90d"})), j, None)


# ------------------------------------------------------- backtest hook


def test_apply_altdata_vetoes_and_flags():
    idx = pd.bdate_range("2026-09-28", periods=5)
    table = pd.DataFrame(
        {"setup": [True, False, True, True, False], "signature": ["s", None, "s", "s", None]},
        index=idx,
    )
    out = apply_altdata(
        {"AAPL": table},
        vetoes={"AAPL": {idx[1].date()}},  # as-of = entry day = the bar after the signal
        flags={"AAPL": {idx[3].date()}},
        flag_name="insider",
    )["AAPL"]
    assert list(out["setup"]) == [False, False, True, True, False]
    assert out["signature"].iloc[2] == "s|insider=1" and out["signature"].iloc[3] == "s|insider=0"
    assert table["setup"].iloc[0]  # input untouched


def test_feature_dates_point_in_time(store):
    from bot.backtest import feature_dates

    idx = pd.bdate_range("2026-09-28", periods=10)
    table = pd.DataFrame({"setup": [True] * 10, "signature": ["s"] * 10}, index=idx)
    store.add_txns([txn("c1", "BOSS", title="CEO", filed=date(2026, 10, 1))])
    dates = feature_dates(store, {"AAPL": table}, "insider_ceo_cfo_buy_30d")["AAPL"]
    # Filed Thu Oct 1 -> usable from entry day Fri Oct 2 onwards (signal Thu Oct 1 close).
    assert min(dates) == date(2026, 10, 2)
    assert date(2026, 10, 1) not in dates


def test_run_backtest_accepts_vetoes():
    import numpy as np

    from bot.backtest import BacktestConfig, run_backtest
    from bot.config import RiskConfig, StrategyConfig

    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2018-01-01", periods=600)

    def bars(drift, vol):
        c = 100 * np.cumprod(1 + rng.normal(drift, vol, len(idx)))
        o = np.concatenate([[c[0]], c[:-1]])
        hi, lo = np.maximum(o, c) * 1.008, np.minimum(o, c) * 0.992
        return pd.DataFrame(
            {"open": o, "high": hi, "low": lo, "close": c, "volume": 2e6}, index=idx
        )

    data = {"SPY": bars(0.0006, 0.008), "S0": bars(0.0012, 0.016)}
    sectors = {"SPY": "Broad", "S0": "A"}
    bt = BacktestConfig(date(2018, 1, 1), date(2020, 6, 30), gated=False)
    base = run_backtest(data, sectors, StrategyConfig(), RiskConfig(), bt)
    everything = {"S0": {d.date() for d in idx}, "SPY": {d.date() for d in idx}}
    vetoed = run_backtest(data, sectors, StrategyConfig(), RiskConfig(), bt, vetoes=everything)
    assert base.signal_trades and not vetoed.signal_trades
