# Author: Kai0421
"""Overnight hold: buy at the 16:00 close, sell at the next 09:30 open.

Traded only through ``TRADING_MODE=overnight`` (``bot.overnight_live``), paper only, by the
owner's decision of 2026-10-08 (memory/decisions.md), which waives the win-rate gate for
it. It stays outside ``bot.strategies`` and ``bot.intraday``: neither registry lists it
and ``STRATEGY`` alone can't select it (tests/test_overnight_hold.py). Known weaknesses:

- its 2021-2026 backtest wins about 50% of nights, well under the 60% floor the other
  modes must meet (docs/19-overnight-hold.md);
- a broker-side stop cannot protect an overnight gap: it only triggers once trading
  resumes, after the open has already gapped through it.

Rules (the "Bot rules" preset of the Overnight Hold Lab dashboard, 2026-10-07):

- **Universe:** ``symbols`` (default: nine large chip stocks).
- **Nights:** every night, or only after a down day, an up day, a close above the
  ``trend_sma``-day average, or every night except Friday (``night_filter``).
- **Ranking:** when more symbols qualify than ``max_positions``, the highest average
  overnight return over the previous ``momentum_nights`` completed nights wins (needs at
  least ``min_momentum_nights`` of them; ties keep ``symbols`` order).
- **Walk-forward universe (optional):** with ``select_top`` > 0, at the start of each
  month only the ``select_top`` symbols with the best average overnight return over their
  last ``select_lookback`` nights completed before that month may trade. No look-ahead:
  a month's list uses only nights that ended before its first session.
- **Size:** ``alloc_pct`` of equity per stock, total capped at 1 - ``cash_reserve_pct``.
- **Cost:** ``cost_bps`` per round trip, taken off each night's return.

Long only, no leverage. Parameters are hypotheses, not tuned.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd

from bot.config import (
    HARD_MIN_WIN_PROB_FLOOR,
    MAX_POSITION_PCT,
    MIN_CASH_RESERVE_PCT,
    MIN_SIGNATURE_TRADES,
    ConfigError,
)
from bot.risk import wilson_lower_bound

NAME: Final = "overnight_hold"
CHIP_BASKET: Final = ("MU", "NVDA", "AMD", "AVGO", "MRVL", "QCOM", "AMAT", "LRCX", "KLAC")
NIGHT_FILTERS: Final = frozenset({"all", "down", "up", "trend", "notfri"})
# Per-stock cap of the live sizing rules (5% of the account per stock).
LIVE_STOCK_CAP: Final = 0.05


@dataclass(frozen=True)
class OvernightConfig:
    symbols: tuple[str, ...] = CHIP_BASKET
    alloc_pct: float = 0.05
    max_positions: int = 3
    cost_bps: float = 10.0
    night_filter: str = "all"
    momentum_nights: int = 20
    min_momentum_nights: int = 10
    trend_sma: int = 50
    cash_reserve_pct: float = 0.02
    select_top: int = 0  # 0 = trade all of ``symbols``; > 0 = walk-forward monthly list
    select_lookback: int = 252

    def __post_init__(self) -> None:
        if not self.symbols or any(s != s.strip().upper() or not s for s in self.symbols):
            raise ConfigError("symbols must be non-empty upper-case tickers")
        if not 0 < self.alloc_pct <= MAX_POSITION_PCT:
            raise ConfigError(f"alloc_pct must be in (0, {MAX_POSITION_PCT}]")
        if self.max_positions < 1:
            raise ConfigError("max_positions must be >= 1")
        if self.cost_bps < 0:
            raise ConfigError("cost_bps must be >= 0")
        if self.night_filter not in NIGHT_FILTERS:
            raise ConfigError(f"night_filter must be one of {sorted(NIGHT_FILTERS)}")
        if not 1 <= self.min_momentum_nights <= self.momentum_nights:
            raise ConfigError("need 1 <= min_momentum_nights <= momentum_nights")
        if self.trend_sma < 2:
            raise ConfigError("trend_sma must be >= 2")
        if not MIN_CASH_RESERVE_PCT <= self.cash_reserve_pct < 1:
            raise ConfigError(f"cash_reserve_pct must be >= {MIN_CASH_RESERVE_PCT}")
        if self.select_top < 0:
            raise ConfigError("select_top must be >= 0")
        if self.select_top and self.select_lookback < 2:
            raise ConfigError("select_lookback must be >= 2")


@dataclass(frozen=True)
class OvernightResult:
    trades: pd.DataFrame  # buy_date, sell_date, symbol, buy, sell, amount, return, pnl
    equity: pd.Series  # account value after each night, indexed by the buy date
    metrics: dict[str, float | int | bool]


def overnight_returns(bars: pd.DataFrame) -> pd.Series:
    """Per bar ``i``: next open / close ``i`` - 1 (NaN on the last bar)."""
    return bars["open"].shift(-1) / bars["close"] - 1


def _features(bars: pd.DataFrame, cfg: OvernightConfig) -> pd.DataFrame:
    on = overnight_returns(bars)
    # Night j is known only after open j+1, so the pick on day i averages nights < i.
    mom = on.shift(1).rolling(cfg.momentum_nights, min_periods=cfg.min_momentum_nights).mean()
    return pd.DataFrame(
        {
            "close": bars["close"],
            "on": on,
            "up": bars["close"] > bars["close"].shift(1),
            "down": bars["close"] < bars["close"].shift(1),
            "sma": bars["close"].rolling(cfg.trend_sma).mean(),
            "mom": mom,
        },
        index=bars.index,
    )


def monthly_selection(
    bars: Mapping[str, pd.DataFrame], cfg: OvernightConfig
) -> dict[pd.Period, tuple[str, ...]] | None:
    """Walk-forward list of tradable symbols per month, or None when ``select_top`` is 0.

    A month's list ranks ``cfg.symbols`` by the mean of their last ``select_lookback``
    nights whose sell date falls before that month, needing at least half of them. Months
    with no symbol meeting that are left out (nothing trades)."""
    if not cfg.select_top:
        return None
    nights: dict[str, pd.Series] = {}
    for s in cfg.symbols:
        b = bars.get(s)
        if b is None or len(b) < 2:
            continue
        on = overnight_returns(b).iloc[:-1]
        on.index = b.index[1:].to_period("M")  # month each night ends in (its sell date)
        nights[s] = on
    months = sorted(set().union(*(b.index.to_period("M") for b in bars.values() if len(b))))
    need = max(cfg.select_lookback // 2, 1)
    out: dict[pd.Period, tuple[str, ...]] = {}
    for m in months:
        ranked: list[tuple[float, int, str]] = []
        for order, s in enumerate(cfg.symbols):
            on = nights.get(s)
            if on is None:
                continue
            past = on[on.index < m].dropna().iloc[-cfg.select_lookback :]
            if len(past) >= need:
                ranked.append((float(past.mean()), order, s))
        if ranked:
            ranked.sort(key=lambda r: (-r[0], r[1]))
            out[m] = tuple(s for _, _, s in ranked[: cfg.select_top])
    return out


def _qualifies(row: pd.Series, day: pd.Timestamp, f: str) -> bool:
    if f == "down":
        return bool(row["down"])
    if f == "up":
        return bool(row["up"])
    if f == "trend":
        return bool(np.isfinite(row["sma"]) and row["close"] > row["sma"])
    if f == "notfri":
        return day.weekday() != 4
    return True


def _pick(
    feats: Mapping[str, pd.DataFrame],
    day: pd.Timestamp,
    cfg: OvernightConfig,
    selection: Mapping[pd.Period, tuple[str, ...]] | None = None,
):
    allowed = None if selection is None else set(selection.get(day.to_period("M"), ()))
    cands: list[tuple[float, int, str]] = []
    for order, sym in enumerate(cfg.symbols):
        if allowed is not None and sym not in allowed:
            continue
        f = feats.get(sym)
        if f is None or day not in f.index:
            continue
        row = f.loc[day]
        if not math.isfinite(row["close"]) or not _qualifies(row, day, cfg.night_filter):
            continue
        mom = row["mom"]
        cands.append((mom if math.isfinite(mom) else -1.0, order, sym))
    cands.sort(key=lambda c: (-c[0], c[1]))
    return [s for _, _, s in cands[: cfg.max_positions]]


def pick_symbols(
    bars: Mapping[str, pd.DataFrame], day: pd.Timestamp, cfg: OvernightConfig
) -> list[str]:
    """Symbols to buy at ``day``'s close, best first. Uses only bars up to ``day``."""
    past = {s: bars[s].loc[:day] for s in cfg.symbols if s in bars}
    feats = {s: _features(b, cfg) for s, b in past.items()}
    return _pick(feats, day, cfg, monthly_selection(past, cfg))


def run_overnight_backtest(
    bars: Mapping[str, pd.DataFrame],
    cfg: OvernightConfig | None = None,
    capital: float = 100_000.0,
) -> OvernightResult:
    """Daily-bar backtest: fills at the official close and next open, no slippage beyond
    ``cost_bps``. Equity compounds night to night."""
    cfg = cfg or OvernightConfig()
    if not capital > 0:
        raise ValueError("capital must be positive")
    feats = {s: _features(bars[s], cfg) for s in cfg.symbols if s in bars}
    selection = monthly_selection({s: bars[s] for s in feats}, cfg)
    days = sorted(set().union(*(f.index for f in feats.values()))) if feats else []
    cost = cfg.cost_bps / 10_000
    equity = capital
    rows, curve = [], {}
    for day in days:
        picks = [
            s for s in _pick(feats, day, cfg, selection) if math.isfinite(feats[s].at[day, "on"])
        ]
        weight = min(cfg.alloc_pct, (1 - cfg.cash_reserve_pct) / len(picks)) if picks else 0.0
        pnl = 0.0
        for s in picks:
            f, b = feats[s], bars[s]
            r = f.at[day, "on"] - cost
            amount = equity * weight
            pnl += amount * r
            nxt = b.index[b.index.get_loc(day) + 1]
            rows.append(
                {
                    "buy_date": day,
                    "sell_date": nxt,
                    "symbol": s,
                    "buy": float(b.at[day, "close"]),
                    "sell": float(b.at[nxt, "open"]),
                    "amount": amount,
                    "return": r,
                    "pnl": amount * r,
                }
            )
        equity += pnl
        curve[day] = equity
    trades = pd.DataFrame(
        rows, columns=["buy_date", "sell_date", "symbol", "buy", "sell", "amount", "return", "pnl"]
    )
    eq = pd.Series(curve, dtype="float64", name="equity")
    return OvernightResult(trades, eq, _metrics(trades, eq, capital, cfg))


def _metrics(trades: pd.DataFrame, eq: pd.Series, capital: float, cfg: OvernightConfig):
    n = len(trades)
    wins = int((trades["return"] > 0).sum())
    win_rate = wins / n if n else 0.0
    lower = wilson_lower_bound(wins, n) if n else 0.0
    avg = float(trades["return"].mean()) if n else 0.0
    final = float(eq.iloc[-1]) if len(eq) else capital
    years = max(len(eq) / 252, 1 / 252)
    peak = eq.cummax() if len(eq) else eq
    max_dd = float((eq / peak - 1).min()) if len(eq) else 0.0
    passes = (
        n >= MIN_SIGNATURE_TRADES
        and lower >= HARD_MIN_WIN_PROB_FLOOR
        and avg > 0
        and cfg.alloc_pct <= LIVE_STOCK_CAP
    )
    return {
        "trades": n,
        "win_rate": win_rate,
        "win_rate_lower_95": lower,
        "avg_return": avg,
        "total_return": final / capital - 1,
        "cagr": (max(final, 1e-9) / capital) ** (1 / years) - 1,
        "max_drawdown": max_dd,
        # The win-rate gate only: the strategy still breaks the stop rule whatever these
        # numbers say (module docstring).
        "passes_gate": bool(passes),
    }


# ------------------------------------------------------------ shadow log (no orders)
# A forward-only record of what the rules would have bought at each close and sold at
# the next open, at real prices. It never places orders; scripts/research/
# overnight_shadow.py keeps it in data/research/overnight_shadow.csv.

SHADOW_COLUMNS: Final = (
    "variant",
    "buy_date",
    "symbol",
    "buy_close",
    "sell_date",
    "sell_open",
    "return",
)


def record_shadow(
    log: pd.DataFrame,
    bars: Mapping[str, pd.DataFrame],
    cfg: OvernightConfig,
    variant: str,
    day: pd.Timestamp,
) -> pd.DataFrame:
    """Append ``day``'s picks at its close, once per day and variant. Days without a bar
    (holidays, or the session not finished yet) are skipped."""
    day = pd.Timestamp(day).normalize()
    done = pd.to_datetime(log["buy_date"]) if len(log) else pd.Series(dtype="datetime64[ns]")
    if len(log) and ((log["variant"] == variant) & (done == day)).any():
        return log
    have = {s: b for s, b in bars.items() if day in b.index}
    if not have:
        return log
    rows = [
        {
            "variant": variant,
            "buy_date": day,
            "symbol": s,
            "buy_close": float(bars[s].at[day, "close"]),
            "sell_date": pd.NaT,
            "sell_open": math.nan,
            "return": math.nan,
        }
        for s in pick_symbols(bars, day, cfg)
        if s in have
    ]
    if not rows:
        return log
    new = pd.DataFrame(rows, columns=list(SHADOW_COLUMNS))
    return new if log.empty else pd.concat([log, new], ignore_index=True)


def settle_shadow(
    log: pd.DataFrame, bars: Mapping[str, pd.DataFrame], cfg: OvernightConfig
) -> pd.DataFrame:
    """Fill the next session's open (less ``cost_bps``) for rows still open."""
    out = log.copy()
    cost = cfg.cost_bps / 10_000
    for i, row in out.iterrows():
        if pd.notna(row["sell_open"]):
            continue
        b = bars.get(row["symbol"])
        if b is None:
            continue
        later = b.index[b.index > pd.Timestamp(row["buy_date"])]
        if not len(later):
            continue
        nxt = later[0]
        sell = float(b.at[nxt, "open"])
        out.at[i, "sell_date"] = nxt
        out.at[i, "sell_open"] = sell
        out.at[i, "return"] = sell / float(row["buy_close"]) - 1 - cost
    return out


def shadow_summary(
    log: pd.DataFrame, cfg: OvernightConfig, capital: float = 100_000.0
) -> pd.DataFrame:
    """Per variant: settled trades, pending, win rate, Wilson lower bound, average return
    and the P&L of a ``capital`` account sized like the backtest."""
    rows = {}
    for variant, g in log.groupby("variant"):
        settled = g[pd.notna(g["return"])]
        n = len(settled)
        wins = int((settled["return"].astype(float) > 0).sum())
        equity = capital
        for _, night in settled.groupby("buy_date", sort=True):
            weight = min(cfg.alloc_pct, (1 - cfg.cash_reserve_pct) / len(night))
            equity += sum(equity * weight * float(r) for r in night["return"])
        rows[variant] = {
            "trades": n,
            "pending": len(g) - n,
            "win_rate": wins / n if n else 0.0,
            "lower_95": wilson_lower_bound(wins, n) if n else 0.0,
            "avg_return_bps": float(settled["return"].astype(float).mean()) * 10_000 if n else 0.0,
            "pnl_on_100k": equity - capital,
        }
    return pd.DataFrame.from_dict(rows, orient="index")
