# Author: Kai0421
"""Tests for bot.risk: probability gate, tradable cash, sizing, buy approval, kill switches.

Cash-check cases follow docs/01-trading-fundamentals.md "Required tests".
"""

import pytest

from bot.config import RiskConfig
from bot.risk import (
    Account,
    BuyRequest,
    KillState,
    PortfolioState,
    approve_buys,
    check_instrument,
    kill_switch,
    probability_gate,
    signature_disabled,
    size_position,
    tradable_cash,
    validate_sell,
    wilson_lower_bound,
)

CFG = RiskConfig()
WHITELIST = frozenset({"AAPL", "MSFT", "JPM", "XOM", "SPY", "TQQQ"})


def account(cash=10_000.0, nmbp=None, equity=None, buying_power=None):
    return Account(
        cash=cash,
        non_marginable_buying_power=cash if nmbp is None else nmbp,
        equity=cash if equity is None else equity,
        buying_power=2 * cash if buying_power is None else buying_power,
    )


def state(acct=None, **kw):
    defaults = dict(
        account=account() if acct is None else acct,
        margin_lock_verified=True,
        unsettled_proceeds=0.0,
        open_buy_orders_cost=0.0,
        open_position_sectors=(),
        kill=KillState(),
        whitelist=WHITELIST,
    )
    defaults.update(kw)
    return PortfolioState(**defaults)


def buy(symbol="AAPL", limit=100.0, stop=96.0, sector="tech", **kw):
    return BuyRequest(symbol=symbol, limit_price=limit, stop_price=stop, sector=sector, **kw)


# ------------------------------------------------------- Wilson lower bound


@pytest.mark.parametrize(
    ("wins", "n", "expected"),
    [(75, 100, 0.65695), (70, 100, 0.60415), (144, 200, 0.65408), (346, 500, 0.65020)],
)
def test_wilson_matches_doc_table(wins, n, expected):
    assert wilson_lower_bound(wins, n) == pytest.approx(expected, abs=1e-5)


@pytest.mark.parametrize(("wins", "n"), [(0, 0), (-1, 10), (11, 10)])
def test_wilson_rejects_bad_input(wins, n):
    with pytest.raises(ValueError):
        wilson_lower_bound(wins, n)


# ------------------------------------------------------- probability gate


def test_gate_passes_doc_minimum():
    g = probability_gate(wins=75, n=100, expectancy_r=0.2, cfg=CFG)
    assert g.passed
    assert g.win_rate == pytest.approx(0.75)
    assert g.lower_bound == pytest.approx(0.65695, abs=1e-5)


def test_gate_rejects_69_of_100_on_lower_bound():
    # 69% point estimate meets the target but the lower bound (~59.4%) fails the floor.
    g = probability_gate(wins=69, n=100, expectancy_r=0.2, cfg=CFG)
    assert not g.passed
    assert "lower bound" in g.reason


def test_gate_passes_70_of_100():
    # Lower bound ~60.4% clears the 60% floor (owner, 2026-10-07).
    assert probability_gate(wins=70, n=100, expectancy_r=0.2, cfg=CFG).passed


@pytest.mark.parametrize(("wins", "n"), [(69, 100), (133, 200), (321, 500)])
def test_gate_rejects_one_win_short_of_table(wins, n):
    assert not probability_gate(wins=wins, n=n, expectancy_r=0.2, cfg=CFG).passed


def test_gate_rejects_too_few_trades():
    g = probability_gate(wins=99, n=99, expectancy_r=1.0, cfg=CFG)
    assert not g.passed
    assert "trades" in g.reason


def test_gate_rejects_non_positive_expectancy():
    for exp in (0.0, -0.1):
        g = probability_gate(wins=90, n=100, expectancy_r=exp, cfg=CFG)
        assert not g.passed
        assert "expectancy" in g.reason


def test_gate_rejects_below_point_target_even_with_large_n():
    # 64% of 10,000: lower bound ~63% passes the 60% floor, but a 65% target fails.
    cfg = RiskConfig(min_win_prob_target=0.65)
    g = probability_gate(wins=6_400, n=10_000, expectancy_r=0.1, cfg=cfg)
    assert not g.passed
    assert "target" in g.reason


def test_gate_default_target_60_passes_61_percent_with_large_n():
    g = probability_gate(wins=6_100, n=10_000, expectancy_r=0.1, cfg=RiskConfig())
    assert g.passed


def test_gate_respects_stricter_config():
    strict = RiskConfig(min_win_prob_target=0.80, min_win_prob_floor=0.75)
    assert not probability_gate(wins=78, n=100, expectancy_r=0.2, cfg=strict).passed


# ------------------------------------------------------- signature disabling


def test_signature_disabled_needs_30_outcomes():
    assert not signature_disabled([False] * 29, CFG)


def test_signature_disabled_when_rolling_win_rate_below_floor():
    outcomes = [True] * 100 + [True] * 17 + [False] * 13  # last 30: 17/30 = 57%
    assert signature_disabled(outcomes, CFG)


def test_signature_kept_at_or_above_floor():
    outcomes = [False] * 50 + [True] * 20 + [False] * 10  # last 30: 20/30 = 67%
    assert not signature_disabled(outcomes, CFG)


# ------------------------------------------------------- tradable cash


def test_doc_worked_example():
    # cash 10,000; unsettled 1,500; open buys 2,000; reserve 2% of 10,000 -> 6,300
    acct = account(cash=10_000, buying_power=20_000)
    assert tradable_cash(acct, 1_500, 2_000, CFG) == pytest.approx(6_300)


def test_buying_power_is_ignored():
    small = tradable_cash(account(cash=5_000, buying_power=5_000), 0, 0, CFG)
    big = tradable_cash(account(cash=5_000, buying_power=50_000), 0, 0, CFG)
    assert small == big


def test_uses_smaller_of_cash_and_non_marginable_bp():
    acct = account(cash=10_000, nmbp=8_000, equity=10_000)
    assert tradable_cash(acct, 0, 0, CFG) == pytest.approx(8_000 - 200)
    acct = account(cash=7_000, nmbp=9_000, equity=10_000)
    assert tradable_cash(acct, 0, 0, CFG) == pytest.approx(7_000 - 200)


@pytest.mark.parametrize(("unsettled", "open_buys"), [(1_000, 0), (0, 1_000)])
def test_each_deduction_is_subtracted(unsettled, open_buys):
    expected = 10_000 - 1_000 - 200
    assert tradable_cash(account(), unsettled, open_buys, CFG) == pytest.approx(expected)


def test_tradable_cash_can_go_negative():
    assert tradable_cash(account(cash=1_000, equity=10_000), 900, 0, CFG) < 0


def test_negative_deductions_rejected():
    with pytest.raises(ValueError):
        tradable_cash(account(), -1, 0, CFG)
    with pytest.raises(ValueError):
        tradable_cash(account(), 0, -1, CFG)


# ------------------------------------------------------- sizing


def test_risk_based_size():
    # 1% of 100k = 1,000 risk; R = 4 -> 250 shares; 20% cap = 20,000 / 100 = 200 shares.
    assert size_position(100_000, 100.0, 96.0, 1_000_000, CFG) == 200
    # Wider stop: R = 10 -> 100 shares (below the 20% cap).
    assert size_position(100_000, 100.0, 90.0, 1_000_000, CFG) == 100


def test_size_capped_by_cash_doc_example():
    # Doc: $250 limit, 6,300 tradable -> at most 25 shares even if risk sizing wants 40.
    # Risk sizing: 1% of 1M / R 5 = 2,000 shares; 20% cap = 800; cash cap = 25.
    assert size_position(1_000_000, 250.0, 245.0, 6_300, CFG) == 25


def test_size_includes_fees():
    # 6,300 cash, $250 limit, $10 fees -> 6,290 / 250 = 25.16 -> 25; $60 fees -> 24.96 -> 24.
    assert size_position(1_000_000, 250.0, 245.0, 6_300, CFG, est_fees=10) == 25
    assert size_position(1_000_000, 250.0, 245.0, 6_300, CFG, est_fees=60) == 24


def test_size_rounds_down():
    assert size_position(100_000, 101.0, 97.0, 1_000_000, CFG) == 198  # 20,000 / 101


@pytest.mark.parametrize("cash", [0.0, -100.0, 50.0])
def test_size_zero_without_enough_cash(cash):
    assert size_position(100_000, 100.0, 96.0, cash, CFG) == 0


@pytest.mark.parametrize(("limit", "stop"), [(100.0, 100.0), (100.0, 101.0), (0.0, -1.0)])
def test_size_rejects_invalid_stop_or_price(limit, stop):
    with pytest.raises(ValueError):
        size_position(100_000, limit, stop, 1_000_000, CFG)


# ------------------------------------------------------- instruments


def test_instrument_allowed():
    assert check_instrument("AAPL", "us_equity", WHITELIST) is None


@pytest.mark.parametrize(
    ("symbol", "asset_class"),
    [("GOOG", "us_equity"), ("AAPL", "crypto"), ("AAPL", "us_option"), ("TQQQ", "us_equity")],
)
def test_instrument_rejected(symbol, asset_class):
    # TQQQ is leveraged: blacklisted even if it slips into the whitelist.
    assert check_instrument(symbol, asset_class, WHITELIST) is not None


# ------------------------------------------------------- kill switches


def test_kill_switch_clear():
    k = kill_switch(equity=99_000, start_of_day_equity=100_000, peak_equity=105_000, cfg=CFG)
    assert not k.halt_new_orders_today and not k.paused


def test_daily_loss_halts_today():
    k = kill_switch(equity=97_000, start_of_day_equity=100_000, peak_equity=100_000, cfg=CFG)
    assert k.halt_new_orders_today and not k.paused


def test_drawdown_pauses():
    k = kill_switch(equity=90_000, start_of_day_equity=90_500, peak_equity=100_000, cfg=CFG)
    assert k.paused


@pytest.mark.parametrize(("eq", "sod", "peak"), [(0, 100, 100), (100, 0, 100), (100, 100, 0)])
def test_kill_switch_fails_closed_on_bad_input(eq, sod, peak):
    assert kill_switch(eq, sod, peak, CFG).paused


# ------------------------------------------------------- sells


def test_sell_within_holdings():
    assert validate_sell(qty=5, held=10, open_sell_qty=5) == 5


@pytest.mark.parametrize(("qty", "held", "open_sell"), [(11, 10, 0), (6, 10, 5), (0, 10, 0)])
def test_sell_rejected(qty, held, open_sell):
    with pytest.raises(ValueError):
        validate_sell(qty=qty, held=held, open_sell_qty=open_sell)


# ------------------------------------------------------- buy approval


def test_approve_single_buy():
    (d,) = approve_buys([buy()], state(account(cash=100_000)), CFG)
    assert d.approved
    assert d.shares == 200
    assert d.cost == pytest.approx(20_000)


def test_account_fetch_failure_blocks_all():
    decisions = approve_buys([buy(), buy("MSFT")], state(account=None), CFG)
    assert all(not d.approved and d.shares == 0 for d in decisions)
    assert "account" in decisions[0].reason


def test_negative_cash_is_critical_and_blocks():
    (d,) = approve_buys([buy()], state(account(cash=-50, nmbp=0, equity=10_000)), CFG)
    assert not d.approved
    assert d.critical


def test_margin_lock_mismatch_blocks():
    (d,) = approve_buys([buy()], state(margin_lock_verified=False), CFG)
    assert not d.approved
    assert d.critical


def test_no_tradable_cash_no_order():
    st = state(account(cash=10_000), open_buy_orders_cost=9_800)
    (d,) = approve_buys([buy()], st, CFG)
    assert not d.approved
    assert d.shares == 0


@pytest.mark.parametrize(
    "kill",
    [KillState(halt_new_orders_today=True, reasons=("daily loss",)), KillState(paused=True)],
)
def test_kill_switch_blocks(kill):
    (d,) = approve_buys([buy()], state(kill=kill), CFG)
    assert not d.approved


def test_batch_total_never_exceeds_cash():
    # 10,000 tradable-ish: each buy wants up to 20% of 100k equity, but cash is the limit.
    acct = account(cash=10_000, nmbp=10_000, equity=100_000)
    reqs = [buy("AAPL"), buy("MSFT", sector="tech2"), buy("JPM", sector="fin")]
    decisions = approve_buys(reqs, state(acct), CFG)
    spent = sum(d.cost for d in decisions)
    assert spent <= tradable_cash(acct, 0, 0, CFG)
    assert decisions[0].approved
    assert [d.approved for d in decisions[1:]] == [False, False]  # 8,000 cash used up


def test_batch_later_orders_shrink():
    acct = account(cash=30_000, nmbp=30_000, equity=100_000)
    reqs = [buy("AAPL"), buy("MSFT", sector="x"), buy("JPM", sector="y")]
    decisions = approve_buys(reqs, state(acct), CFG)
    # tradable = 30,000 - 2,000 = 28,000: 200 + 80 shares, then nothing.
    assert [d.shares for d in decisions] == [200, 80, 0]
    assert sum(d.cost for d in decisions) <= 28_000


def test_max_open_positions():
    st = state(account(cash=1_000_000), open_position_sectors=("a", "b", "c", "d", "e", "f"))
    (d,) = approve_buys([buy()], st, CFG)
    assert not d.approved
    assert "positions" in d.reason


def test_max_per_sector_counts_batch():
    st = state(account(cash=1_000_000), open_position_sectors=("tech",))
    decisions = approve_buys([buy("AAPL"), buy("MSFT")], st, CFG)
    assert [d.approved for d in decisions] == [True, False]
    assert "sector" in decisions[1].reason


def test_missing_sector_rejected():
    (d,) = approve_buys([buy(sector="")], state(), CFG)
    assert not d.approved


def test_non_whitelisted_rejected_in_batch():
    (d,) = approve_buys([buy("GOOG")], state(account(cash=100_000)), CFG)
    assert not d.approved


def test_invalid_stop_rejected_not_raised():
    (d,) = approve_buys([buy(stop=101.0)], state(), CFG)
    assert not d.approved
    assert "stop" in d.reason


def test_duplicate_symbol_in_batch_rejected():
    st = state(account(cash=1_000_000))
    decisions = approve_buys([buy("AAPL"), buy("AAPL", sector="other")], st, CFG)
    assert [d.approved for d in decisions] == [True, False]
