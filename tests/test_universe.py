# Author: Kai0421
"""Tests for bot.universe: the default S&P 100 list and the opt-in S&P 500 list."""

import pytest

from bot.universe import BENCHMARK, UNIVERSE, sector_of, symbols, universe_for

SECTORS = {
    "Information Technology", "Communication Services", "Consumer Discretionary",
    "Consumer Staples", "Health Care", "Financials", "Industrials", "Energy", "Utilities",
    "Real Estate", "Materials", "Broad Market",
}  # fmt: skip


def test_default_universe_is_unchanged():
    assert universe_for() == UNIVERSE
    assert symbols() == sorted(UNIVERSE)
    assert sector_of("AAPL") == "Information Technology"


def test_sp500_contains_sp100_and_its_etfs_with_known_sectors():
    wide = universe_for("sp500")
    assert len(wide) > 490
    assert set(UNIVERSE) <= set(wide)
    assert {s: wide[s] for s in UNIVERSE} == UNIVERSE  # S&P 100 sectors win
    assert BENCHMARK in wide
    assert set(wide.values()) <= SECTORS
    assert sector_of("DECK", "sp500") == "Consumer Discretionary"
    assert sector_of("DECK") == ""


def test_sp500_lists_no_leveraged_or_inverse_products():
    assert not {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SH", "SDS"} & set(universe_for("sp500"))


def test_unknown_universe_is_an_error():
    with pytest.raises(ValueError, match="unknown universe"):
        universe_for("nasdaq")
