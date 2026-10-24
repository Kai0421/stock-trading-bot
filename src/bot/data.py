# Author: Kai0421
"""Daily bars from Alpaca market data, cleaned and cached as CSV under ``data/bars/``.

- Bars are split- and dividend-adjusted (``Adjustment.ALL``) and indexed by their
  America/New_York session date (naive ``Timestamp``), matching the strategy's frames.
- The SIP feed is the default: Alpaca's free plan serves historical SIP bars except the
  latest 15 minutes, which never matters for daily backtests ending yesterday.
- Bad data fails closed per symbol: the symbol is skipped (and reported), never repaired.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot.config import ConfigError, Settings

ET = ZoneInfo("America/New_York")
OHLCV = ["open", "high", "low", "close", "volume"]
DEFAULT_CACHE_DIR = Path("data/bars")


def bars_request(
    symbols: Sequence[str], start: date, end: date | datetime, feed: str = "sip"
) -> Any:
    """Adjusted daily bars from ``start`` to ``end`` (ET).

    ``end`` may be a date (through 23:59 ET) or an aware datetime, e.g. now - 16 minutes:
    the free plan refuses SIP data from the latest 15 minutes.
    """
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end_dt = end if isinstance(end, datetime) else datetime.combine(end, time(23, 59), tzinfo=ET)
    if end_dt.astimezone(ET).date() < start:
        raise ValueError(f"end {end} is before start {start}")
    return StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, time(0, 0), tzinfo=ET),
        end=end_dt,
        adjustment=Adjustment.ALL,
        feed=DataFeed(feed),
    )


def barset_to_frames(barset: Any) -> dict[str, pd.DataFrame]:
    """Convert an Alpaca ``BarSet`` into one OHLCV frame per symbol (uncleaned)."""
    frames: dict[str, pd.DataFrame] = {}
    for symbol, rows in (barset.data or {}).items():
        if not rows:
            continue
        index = pd.DatetimeIndex(
            [pd.Timestamp(r.timestamp.astimezone(ET).date()) for r in rows], name=None
        )
        frames[symbol] = pd.DataFrame(
            {
                "open": [r.open for r in rows],
                "high": [r.high for r in rows],
                "low": [r.low for r in rows],
                "close": [r.close for r in rows],
                "volume": [r.volume for r in rows],
            },
            index=index,
            dtype="float64",
        )
    return frames


def clean_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Validate and sort bars; raise ``ValueError`` on anything suspicious."""
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        raise ValueError(f"{symbol}: missing columns {missing}")
    out = df.loc[:, OHLCV].astype("float64").sort_index()
    out.index = pd.DatetimeIndex(out.index).as_unit("ns")
    values = out.to_numpy()
    if np.isnan(values).any():
        raise ValueError(f"{symbol}: NaN in bars")
    if not out.index.is_unique:
        raise ValueError(f"{symbol}: duplicate dates")
    if (out[["open", "high", "low", "close"]] <= 0).to_numpy().any():
        raise ValueError(f"{symbol}: non-positive price")
    if (out["volume"] < 0).any():
        raise ValueError(f"{symbol}: negative volume")
    if (out["high"] < out["low"]).any():
        raise ValueError(f"{symbol}: high below low")
    return out


class BarCache:
    """One CSV per symbol plus a JSON sidecar recording the requested date range."""

    def __init__(self, directory: Path | str = DEFAULT_CACHE_DIR) -> None:
        self.dir = Path(directory)

    def _paths(self, symbol: str) -> tuple[Path, Path]:
        safe = symbol.replace("/", "_")
        return self.dir / f"{safe}.csv", self.dir / f"{safe}.json"

    def read(self, symbol: str) -> pd.DataFrame | None:
        csv, _ = self._paths(symbol)
        if not csv.exists():
            return None
        df = pd.read_csv(csv, index_col=0, parse_dates=True)
        df.index = pd.DatetimeIndex(df.index).as_unit("ns").rename(None)
        return df.astype("float64")

    def covers(self, symbol: str, start: date, end: date) -> bool:
        csv, meta = self._paths(symbol)
        if not (csv.exists() and meta.exists()):
            return False
        span = json.loads(meta.read_text())
        return date.fromisoformat(span["start"]) <= start and date.fromisoformat(span["end"]) >= end

    def write(self, symbol: str, df: pd.DataFrame, start: date, end: date) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        csv, meta = self._paths(symbol)
        df.to_csv(csv, index_label="date")
        meta.write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat()}))


@dataclass
class LoadResult:
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)  # symbol -> reason


def _trim(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    return df.loc[pd.Timestamp(start) : pd.Timestamp(end)]


def load_daily_bars(
    client: Any,
    symbols: Sequence[str],
    start: date,
    end: date | datetime,
    cache: BarCache | None,
    feed: str = "sip",
    batch: int = 50,
) -> LoadResult:
    """Bars for ``symbols`` from cache when it covers the range, otherwise from Alpaca.

    ``cache=None`` always fetches and stores nothing (used for same-day EOD bars).
    """
    end_day = end.astimezone(ET).date() if isinstance(end, datetime) else end
    result = LoadResult()
    to_fetch: list[str] = []
    for symbol in symbols:
        covered = cache is not None and cache.covers(symbol, start, end_day)
        cached = cache.read(symbol) if covered and cache is not None else None
        if cached is None:
            to_fetch.append(symbol)
        else:
            result.bars[symbol] = _trim(cached, start, end_day)

    for i in range(0, len(to_fetch), batch):
        chunk = to_fetch[i : i + batch]
        frames = barset_to_frames(client.get_stock_bars(bars_request(chunk, start, end, feed)))
        for symbol in chunk:
            if symbol not in frames:
                result.skipped[symbol] = "no bars returned"
                continue
            try:
                df = clean_bars(frames[symbol], symbol)
            except ValueError as exc:
                result.skipped[symbol] = str(exc)
                continue
            if cache is not None:
                cache.write(symbol, df, start, end_day)
            result.bars[symbol] = _trim(df, start, end_day)
    return result


def intraday_bars_request(
    symbols: Sequence[str], start: datetime, end: datetime, minutes: int = 5, feed: str = "sip"
) -> Any:
    """Adjusted ``minutes``-minute bars between two aware datetimes (intraday mode)."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("intraday bars need aware start < end")
    return StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=DataFeed(feed),
    )


def barset_to_intraday_frames(barset: Any) -> dict[str, pd.DataFrame]:
    """One OHLCV frame per symbol indexed by bar start (ET, tz-naive), regular hours only
    (09:30 to 16:00); pre- and after-market bars are dropped."""
    frames: dict[str, pd.DataFrame] = {}
    for symbol, rows in (barset.data or {}).items():
        if not rows:
            continue
        index = pd.DatetimeIndex(
            [pd.Timestamp(r.timestamp.astimezone(ET).replace(tzinfo=None)) for r in rows]
        )
        df = pd.DataFrame(
            {
                "open": [r.open for r in rows],
                "high": [r.high for r in rows],
                "low": [r.low for r in rows],
                "close": [r.close for r in rows],
                "volume": [r.volume for r in rows],
            },
            index=index,
            dtype="float64",
        )
        minute = index.hour * 60 + index.minute
        frames[symbol] = df[(minute >= 9 * 60 + 30) & (minute < 16 * 60)]
    return frames


def load_intraday_bars(
    client: Any,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    minutes: int = 5,
    feed: str = "sip",
    batch: int = 1,
) -> LoadResult:
    """Regular-hours ``minutes``-minute bars, cleaned; bad symbols are skipped (fail
    closed). Not cached: the live path wants today's bars, backtests fetch once."""
    result = LoadResult()
    for i in range(0, len(symbols), batch):
        chunk = list(symbols[i : i + batch])
        req = intraday_bars_request(chunk, start, end, minutes, feed)
        frames = barset_to_intraday_frames(client.get_stock_bars(req))
        for symbol in chunk:
            if symbol not in frames or frames[symbol].empty:
                result.skipped[symbol] = "no bars returned"
                continue
            try:
                result.bars[symbol] = clean_bars(frames[symbol], symbol)
            except ValueError as exc:
                result.skipped[symbol] = str(exc)
    return result


def latest_prices(client: Any, symbols: Sequence[str], feed: str = "iex") -> dict[str, float]:
    """Last trade price per symbol (IEX is real-time on the free plan). Errors -> {}."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestTradeRequest

    try:
        trades = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed(feed))
        )
    except Exception:
        return {}
    return {
        s: float(t.price) for s, t in trades.items() if t is not None and t.price and t.price > 0
    }


def make_data_client(settings: Settings) -> Any:
    """Alpaca historical data client. Missing keys are a configuration error."""
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for market data")
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)
