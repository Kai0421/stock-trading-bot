# Author: Kai0421
"""Tests for bot.desk: rule-based analysts, the judge and point-in-time inputs."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.altdata.edgar import EdgarClient
from bot.backtest import BacktestConfig, run_backtest
from bot.config import RiskConfig
from bot.desk import DeskInputs, analyze, main
from bot.desk.analysts import (
    fundamentals,
    insiders,
    market,
    market_frame,
    news,
    technical,
    technical_frame,
)
from bot.desk.facts import AnnualFacts, annual_facts
from bot.desk.judge import RATINGS, Report, decide, rating_for
from bot.strategies import ExitAction, PositionState, get_strategy
from bot.strategies.analyst_desk import AnalystDesk

AS_OF = date(2026, 10, 5)


def bars(daily_return: float, n: int = 300, end: str = "2026-10-02", noise: float = 0.0):
    idx = pd.bdate_range(end=end, periods=n)
    rng = np.random.default_rng(0)
    steps = daily_return + noise * rng.standard_normal(n)
    close = 100 * np.cumprod(1 + steps)
    return pd.DataFrame(
        {"open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
         "volume": 1e6},
        index=idx,
    )  # fmt: skip


def report(name: str, score: float, *flags: str, available: bool = True) -> Report:
    return Report(name, score, ((score, f"{name} point"),), available, flags=frozenset(flags))


# ------------------------------------------------------------------ technical


def test_technical_uptrend_is_bullish_and_downtrend_bearish():
    up = technical(bars(0.001), AS_OF)
    down = technical(bars(-0.001), AS_OF)
    assert up.available and up.score > 0.3
    assert down.available and down.score < -0.3
    assert any("200-day" in text for _, text in up.points)


def test_technical_needs_a_year_of_bars():
    r = technical(bars(0.001, n=100), AS_OF)
    assert not r.available and r.score == 0


def test_technical_ignores_bars_on_or_after_as_of():
    """A crash bar dated on the as-of day itself must not change the premarket view."""
    b = bars(0.001, end="2026-10-05")
    before = technical(b.iloc[:-1], AS_OF)
    crashed = b.copy()
    crashed.iloc[-1, :4] = crashed.iloc[-1, :4] * 0.5
    assert technical(crashed, AS_OF) == before


def test_technical_flags_high_volatility():
    r = technical(bars(0.001, noise=0.05), AS_OF)
    assert "high_volatility" in r.flags


def test_market_below_200_day_is_flagged():
    r = market(bars(-0.001), AS_OF)
    assert r.score < 0 and "market_downtrend" in r.flags
    assert "market_downtrend" not in market(bars(0.001), AS_OF).flags


# ------------------------------------------------------------------ fundamentals


def facts_json(entries):
    def unit(rows):
        return [
            {"start": s, "end": e, "val": v, "filed": f, "form": form, "fp": "FY"}
            for s, e, v, f, form in rows
        ]

    out = {"facts": {"us-gaap": {}}}
    for tag, u, rows in entries:
        out["facts"]["us-gaap"][tag] = {"units": {u: unit(rows)}}
    return out


FACTS = facts_json(
    [
        ("Revenues", "USD", [
            ("2023-01-01", "2023-12-31", 100.0, "2024-02-01", "10-K"),
            ("2024-01-01", "2024-12-31", 120.0, "2025-02-01", "10-K"),
            # Restated in a later 10-K: the originally filed value is what was known.
            ("2024-01-01", "2024-12-31", 90.0, "2026-02-01", "10-K"),
            ("2025-01-01", "2025-12-31", 150.0, "2026-02-01", "10-K"),
            ("2025-07-01", "2025-09-30", 40.0, "2025-11-01", "10-Q"),
        ]),
        ("NetIncomeLoss", "USD", [
            ("2024-01-01", "2024-12-31", 24.0, "2025-02-01", "10-K"),
            ("2025-01-01", "2025-12-31", 30.0, "2026-02-01", "10-K"),
        ]),
        ("EarningsPerShareDiluted", "USD/shares", [
            ("2024-01-01", "2024-12-31", 2.0, "2025-02-01", "10-K"),
            ("2025-01-01", "2025-12-31", 2.5, "2026-02-01", "10-K"),
        ]),
    ]
)  # fmt: skip


def test_annual_facts_are_point_in_time():
    early = annual_facts(FACTS, date(2025, 6, 1))
    assert [v for _, v in early.revenue] == [120.0, 100.0]  # 2025 10-K not filed yet
    late = annual_facts(FACTS, date(2026, 6, 1))
    # 2024 keeps its original value; quarterly rows are ignored.
    assert [v for _, v in late.revenue] == [150.0, 120.0, 100.0]
    assert late.net_income[0] == (date(2025, 12, 31), 30.0)
    assert late.eps[0][1] == 2.5


def test_annual_facts_fall_back_across_revenue_tags():
    j = facts_json(
        [("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", [
            ("2024-01-01", "2024-12-31", 10.0, "2025-02-01", "10-K"),
            ("2025-01-01", "2025-12-31", 12.0, "2026-02-01", "10-K"),
        ])]
    )  # fmt: skip
    assert [v for _, v in annual_facts(j, AS_OF).revenue] == [12.0, 10.0]


def test_fundamentals_growth_and_margin():
    good = fundamentals(annual_facts(FACTS, AS_OF))
    assert good.available and good.score > 0.4
    shrinking = AnnualFacts(
        revenue=((date(2025, 12, 31), 80.0), (date(2024, 12, 31), 100.0)),
        net_income=((date(2025, 12, 31), -5.0),),
        eps=(),
    )
    assert fundamentals(shrinking).score < -0.4


def test_fundamentals_unavailable_without_two_years():
    assert not fundamentals(None).available
    one = AnnualFacts(revenue=((date(2025, 12, 31), 80.0),), net_income=(), eps=())
    assert not fundamentals(one).available


def test_edgar_company_facts_url():
    calls = []

    class Resp:
        status_code = 200

        def json(self):
            return {"facts": {}}

    def get(url, headers, timeout):
        calls.append(url)
        return Resp()

    client = EdgarClient("test test@example.com", get=get, sleep=lambda s: None)
    assert client.company_facts(320193) == {"facts": {}}
    assert calls == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"]


# ------------------------------------------------------------------ insiders and news


def test_insiders_maps_flags():
    assert not insiders(None).available
    quiet = insiders({"insider_cluster_buy_90d": False})
    assert quiet.available and quiet.score == 0
    buying = insiders({"insider_cluster_buy_90d": True, "insider_ceo_cfo_buy_30d": True})
    selling = insiders({"insider_heavy_selling_30d": True})
    assert buying.score > 0.5 and selling.score < 0


def test_news_tone_and_shock():
    assert not news(None).available
    assert news([]).score == 0
    upbeat = news(["Acme beats estimates, raises guidance", "Analyst upgrades Acme"])
    assert upbeat.score > 0 and not upbeat.flags
    shock = news(["Acme shares fall", "SEC investigation into Acme accounting"])
    assert shock.score < 0 and "news_shock" in shock.flags


# ------------------------------------------------------------------ judge


@pytest.mark.parametrize(
    ("score", "rating"),
    [(0.6, "Buy"), (0.3, "Overweight"), (0.0, "Hold"), (-0.3, "Underweight"), (-0.6, "Sell")],
)
def test_rating_thresholds(score, rating):
    assert rating_for(score) == rating


def test_decide_weights_available_analysts_only():
    d = decide([
        report("technical", 0.8), report("market", 0.8), report("fundamentals", 0.8),
        report("insiders", -1.0, available=False), report("news", 0.8),
    ])  # fmt: skip
    assert d.rating == "Buy" and d.score == pytest.approx(0.8)
    assert d.bull and not d.bear


@pytest.mark.parametrize(
    ("flag", "cap"),
    [("news_shock", "Underweight"), ("market_downtrend", "Hold"), ("high_volatility", "Hold")],
)
def test_risk_flags_cap_the_rating(flag, cap):
    reports = [report(n, 0.9) for n in ("technical", "market", "fundamentals", "insiders")]
    d = decide([*reports, report("news", 0.9, flag)])
    assert d.rating == cap
    assert RATINGS.index(d.rating) >= RATINGS.index(cap)
    assert d.caps


def test_too_little_data_caps_at_hold():
    d = decide([
        report("technical", 0.9), report("market", 0.9),
        report("fundamentals", 0, available=False), report("insiders", 0, available=False),
        report("news", 0, available=False),
    ])  # fmt: skip
    assert d.rating == "Hold"


def test_caps_never_raise_a_bearish_rating():
    d = decide([report(n, -0.9, "market_downtrend") for n in
                ("technical", "market", "fundamentals", "insiders", "news")])  # fmt: skip
    assert d.rating == "Sell"


# ------------------------------------------------------------------ end to end


def test_analyze_and_markdown():
    inputs = DeskInputs(
        bars=bars(0.001),
        spy_bars=bars(0.0005),
        facts=annual_facts(FACTS, AS_OF),
        insider_flags={"insider_cluster_buy_90d": True},
        headlines=["Acme beats estimates"],
    )
    d = analyze("ACME", AS_OF, inputs)
    assert d.rating in ("Buy", "Overweight")
    md = d.to_markdown("ACME", AS_OF)
    assert "ACME" in md and d.rating in md and "Bull case" in md and "Bear case" in md
    assert set(d.to_dict()) >= {"rating", "score", "confidence", "analysts"}


def test_main_rejects_bad_date(capsys):
    assert main(["ACME", "--date", "not-a-date"]) == 2


# ------------------------------------------------------------------ as a pluggable strategy


def test_technical_frame_matches_the_single_day_analyst():
    b = bars(0.001, noise=0.01)
    frame = technical_frame(b)
    assert frame["tech_score"].iloc[-1] == pytest.approx(technical(b, AS_OF).score)
    assert frame["tech_score"].iloc[:251].isna().all()
    m = market_frame(b)
    assert m["mkt_score"].iloc[-1] == pytest.approx(market(b, AS_OF).score)


def desk_market(n=700, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)

    def ohlcv(drift, vol):
        c = 100 * np.cumprod(1 + rng.normal(drift, vol, n))
        return pd.DataFrame(
            {"open": c, "high": c * 1.008, "low": c * 0.992, "close": c, "volume": 2e6},
            index=idx,
        )

    out = {"SPY": ohlcv(0.0006, 0.008)}
    out.update({f"S{k}": ohlcv(0.0008, 0.015) for k in range(4)})
    return out, {s: f"X{i}" for i, s in enumerate(out)}


def test_registry_includes_the_desk():
    assert isinstance(get_strategy("analyst_desk"), AnalystDesk)


def test_desk_setups_are_upgrades_with_no_look_ahead():
    data, _ = desk_market()
    s = AnalystDesk()
    _, full = s.prepare(data, "SPY")
    cut = data["SPY"].index[500]
    _, prefix = s.prepare({k: v.loc[:cut] for k, v in data.items()}, "SPY")
    for sym in data:
        pd.testing.assert_frame_equal(prefix[sym], full[sym].loc[:cut])
    t = full["S0"]
    setups = t.index[t["setup"]]
    assert len(setups) > 0
    for day in setups:  # bullish today, not bullish the session before
        i = t.index.get_loc(day)
        assert t["bullish"].iloc[i] and not t["bullish"].iloc[i - 1]
    assert all(x.startswith("analyst_desk|") for x in t["signature"].dropna())


def test_desk_exits():
    s = AnalystDesk()
    pos = PositionState("X", 100.0, 95.0, 95.0, 100, 1, False)
    assert s.manage(pos, {"close": 99.0, "tech_score": -0.5}) == (
        [ExitAction("downgrade", 100)],
        95.0,
    )
    late = PositionState("X", 100.0, 95.0, 95.0, 100, 20, False)
    assert s.manage(late, {"close": 99.0, "tech_score": 0.3})[0] == [ExitAction("time_stop", 100)]
    assert s.manage(pos, {"close": 99.0, "tech_score": 0.3}) == ([], 95.0)


def test_desk_backtest_runs_end_to_end():
    data, sectors = desk_market()
    bt = BacktestConfig(start=date(2019, 1, 1), end=date(2020, 9, 30), gated=False)
    res = run_backtest(data, sectors, AnalystDesk(), RiskConfig(), bt)
    assert res.strategy == "analyst_desk" and res.signal_trades
    assert {t.exit_reason for t in res.signal_trades} <= {
        "stop", "downgrade", "time_stop", "end_of_data",
    }  # fmt: skip
