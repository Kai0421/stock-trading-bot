# Author: Kai0421
"""Tradable universe: S&P 100 constituents plus liquid, unleveraged ETFs, with GICS sectors.

``UNIVERSE=sp500`` (opt-in) widens it to the S&P 500 plus the same ETFs; the default
stays ``sp100``. Evidence records the universe it was backtested on, and the engine
refuses evidence from a different universe.

The constituent list reflects the S&P 100 as of ~2025 and must be checked against the
current index before live use. Backtests on today's constituents carry survivorship
bias (names that dropped out are missing); reports say so.

Sector ETFs share their underlying sector, so XLK and AAPL count towards the same
max-per-sector limit. Leveraged / inverse products are never listed here (and bot.risk
rejects them anyway).
"""

from __future__ import annotations

from typing import Final

BENCHMARK: Final = "SPY"

_IT = "Information Technology"
_COMM = "Communication Services"
_DISC = "Consumer Discretionary"
_STAP = "Consumer Staples"
_HC = "Health Care"
_FIN = "Financials"
_IND = "Industrials"
_ENE = "Energy"
_UTIL = "Utilities"
_RE = "Real Estate"
_MAT = "Materials"
_BROAD = "Broad Market"

_SECTOR_MEMBERS: Final[dict[str, tuple[str, ...]]] = {
    _IT: (
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "ACN", "CSCO", "AMD", "IBM",
        "INTU", "NOW", "QCOM", "TXN", "INTC", "PLTR", "XLK",
    ),
    _COMM: ("GOOGL", "GOOG", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR", "XLC"),
    _DISC: ("AMZN", "TSLA", "HD", "MCD", "LOW", "NKE", "SBUX", "BKNG", "GM", "F", "XLY"),
    _STAP: ("PG", "KO", "PEP", "COST", "WMT", "MO", "PM", "CL", "MDLZ", "TGT", "XLP"),
    _HC: (
        "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "DHR", "AMGN", "PFE", "BMY", "GILD",
        "CVS", "MDT", "ISRG", "XLV",
    ),
    _FIN: (
        "BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK", "SCHW", "COF",
        "MET", "AIG", "BK", "USB", "PYPL", "XLF",
    ),
    _IND: (
        "GE", "CAT", "HON", "UNP", "UPS", "RTX", "BA", "LMT", "DE", "MMM", "GD", "EMR", "FDX",
        "UBER", "XLI",
    ),
    _ENE: ("XOM", "CVX", "COP", "XLE"),
    _UTIL: ("NEE", "DUK", "SO", "XLU"),
    _RE: ("AMT", "SPG", "XLRE"),
    _MAT: ("LIN", "XLB"),
    _BROAD: ("SPY", "QQQ"),
}  # fmt: skip

UNIVERSE: Final[dict[str, str]] = {
    symbol: sector for sector, members in _SECTOR_MEMBERS.items() for symbol in members
}


DEFAULT_UNIVERSE: Final = "sp100"
UNIVERSE_NAMES: Final = frozenset({"sp100", "sp500"})


def universe_for(name: str = DEFAULT_UNIVERSE) -> dict[str, str]:
    """Symbol -> sector for a named universe ("sp100" or "sp500")."""
    if name == "sp100":
        return dict(UNIVERSE)
    if name == "sp500":
        from bot.universe_sp500 import SP500_MEMBERS

        wide = {s: sector for sector, members in SP500_MEMBERS.items() for s in members}
        return {**wide, **UNIVERSE}  # ETFs and the S&P 100's own sectors win
    raise ValueError(f"unknown universe {name!r}; one of {sorted(UNIVERSE_NAMES)}")


def symbols(name: str = DEFAULT_UNIVERSE) -> list[str]:
    """All universe symbols, benchmark included, in a stable order."""
    return sorted(universe_for(name))


def sector_of(symbol: str, name: str = DEFAULT_UNIVERSE) -> str:
    """GICS sector, or "" when unknown (bot.risk rejects unknown sectors)."""
    return universe_for(name).get(symbol, "")
