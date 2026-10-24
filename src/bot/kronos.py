# Author: Kai0421
"""Kronos forecasts for the ``kronos_rank`` strategy: a cache file and the job that fills it.

Kronos (https://github.com/shiyu-coder/Kronos, MIT) is a small transformer that forecasts
candlesticks. This module is a data job, like ``bot.gather``: it reads daily bars and writes
forecasts to a CSV; it never touches the broker. The strategy (``bot.strategies.kronos_rank``)
only reads the CSV, so the decision layer stays pure and the bot runs without torch.

Each forecast is made on one day's close from the ``lookback`` daily bars ending that day (no
look-ahead), runs ``samples`` sampled paths ``horizon`` trading days ahead with a fixed seed,
and stores ``p_up`` (share of paths that close above the last close) and ``fret`` (mean
forecast return). Rows are keyed by (symbol, date, model, seed); missing rows mean "no
forecast" and the strategy takes no setup (fail closed).

Off unless ``STRATEGY=kronos_rank``. Needs the optional ``kronos`` dependency group and a
checkout of the Kronos repo (``KRONOS_SRC``, default ``~/Development/Kronos``):

    uv sync --group kronos
    uv run --group kronos python -m bot.kronos --start 2025-09-02 --end 2026-10-09

Research results (2026-10-09, memory/decisions.md): zero-shot Kronos-small showed no
reliable edge on our signals; this strategy exists to test it on paper, not because it works.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_FORECASTS = Path("data/kronos/forecasts.csv")
DEFAULT_SRC = Path("~/Development/Kronos")
DEFAULT_MODEL = "NeoQuasar/Kronos-small"
DEFAULT_TOKENIZER = "NeoQuasar/Kronos-Tokenizer-base"
FIELDS = ("symbol", "date", "model", "seed", "p_up", "fret")


@dataclass(frozen=True)
class Forecast:
    symbol: str
    day: date
    model: str
    seed: int
    p_up: float
    fret: float


# ------------------------------------------------------------------ cache


def read_forecasts(path: Path | str, model: str = DEFAULT_MODEL, seed: int = 0) -> pd.DataFrame:
    """Cached forecasts for one model and seed as a frame indexed by (symbol, date), with
    columns ``p_up`` and ``fret``. A missing file is an empty frame."""
    p = Path(path)
    empty = pd.DataFrame(
        {"p_up": pd.Series(dtype="float64"), "fret": pd.Series(dtype="float64")},
        index=pd.MultiIndex.from_tuples([], names=["symbol", "date"]),
    )
    if not p.exists():
        return empty
    df = pd.read_csv(p, dtype={"symbol": str, "model": str})
    df = df[(df["model"] == model) & (df["seed"] == seed)]
    if df.empty:
        return empty
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.drop_duplicates(["symbol", "date"], keep="last")
    return df.set_index(["symbol", "date"])[["p_up", "fret"]].astype("float64").sort_index()


def append_forecasts(path: Path | str, rows: Iterable[Forecast]) -> int:
    """Append forecasts to the cache, writing the header for a new file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    n = 0
    with p.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FIELDS)
        for r in rows:
            w.writerow(
                [r.symbol, r.day.isoformat(), r.model, r.seed, f"{r.p_up:.6f}", f"{r.fret:.6f}"]
            )
            n += 1
    return n


# ------------------------------------------------------------------ forecasting


def summarize(paths: np.ndarray, last_close: float) -> tuple[float, float]:
    """``(p_up, fret)`` from sampled closes at the horizon (one per path)."""
    closes = np.asarray(paths, dtype="float64")
    if closes.size == 0 or not last_close > 0 or not np.isfinite(closes).all():
        raise ValueError("need finite sampled closes and a positive last close")
    return float((closes > last_close).mean()), float(closes.mean() / last_close - 1)


# predict(window, future_index, samples, seed) -> horizon closes, one per sampled path
Predictor = Callable[[pd.DataFrame, pd.DatetimeIndex, int, int], np.ndarray]


def forecast_symbol(
    symbol: str,
    bars: pd.DataFrame,
    days: Sequence[date],
    predict: Predictor,
    future_days: Callable[[date, int], pd.DatetimeIndex],
    *,
    model: str = DEFAULT_MODEL,
    seed: int = 0,
    lookback: int = 400,
    horizon: int = 5,
    samples: int = 30,
) -> list[Forecast]:
    """One forecast per day in ``days`` that has ``lookback`` bars ending on it."""
    idx = pd.DatetimeIndex(bars.index)
    out = []
    for day in days:
        end = int(np.searchsorted(idx.date, day, side="right"))
        if end < lookback or idx[end - 1].date() != day:
            continue
        window = bars.iloc[end - lookback : end]
        closes = predict(window, future_days(day, horizon), samples, seed)
        p_up, fret = summarize(closes, float(window["close"].iloc[-1]))
        out.append(Forecast(symbol, day, model, seed, p_up, fret))
    return out


def kronos_predictor(src: Path, model: str, tokenizer: str) -> Predictor:  # pragma: no cover
    """A ``Predictor`` backed by Kronos. Needs torch (``uv sync --group kronos``) and the Kronos
    repo at ``src``. Keeps every sampled path (KronosPredictor.predict averages them)."""
    import torch

    sys.path.insert(0, str(src.expanduser()))
    from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore[import-not-found]
    from model.kronos import auto_regressive_inference, calc_time_stamps  # type: ignore

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    pred = KronosPredictor(
        Kronos.from_pretrained(model), KronosTokenizer.from_pretrained(tokenizer), device=device
    )
    cols = ["open", "high", "low", "close", "volume", "amount"]

    def predict(window: pd.DataFrame, future: pd.DatetimeIndex, samples: int, seed: int):
        # Same preprocessing as KronosPredictor.predict, but the window is repeated once per
        # sample along the batch axis (sample_count=1), so every sampled path comes back.
        df = window.loc[:, ["open", "high", "low", "close", "volume"]].astype("float64")
        df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
        x = df[cols].to_numpy(dtype=np.float32)
        mean, std = x.mean(axis=0), x.std(axis=0)
        xn = np.clip((x - mean) / (std + 1e-5), -pred.clip, pred.clip)
        x_ts = calc_time_stamps(pd.Series(pd.DatetimeIndex(window.index).tz_localize(None)))
        y_ts = calc_time_stamps(pd.Series(future.tz_localize(None)))

        def batch(a: Any) -> Any:
            arr = np.repeat(np.asarray(a, dtype=np.float32)[np.newaxis], samples, axis=0)
            return torch.from_numpy(arr).to(device)

        torch.manual_seed(seed)
        out = auto_regressive_inference(
            pred.tokenizer,
            pred.model,
            batch(xn),
            batch(x_ts.values),
            batch(y_ts.values),
            pred.max_context,
            len(future),
            pred.clip,
            1.0,
            0,
            0.9,
            1,
            False,
        )
        close = cols.index("close")
        return out[:, -1, close] * (std[close] + 1e-5) + mean[close]

    return predict


# ------------------------------------------------------------------ CLI


def _trading_days(client: Any, start: date, end: date) -> list[date]:  # pragma: no cover
    from alpaca.trading.requests import GetCalendarRequest

    cal = client.get_calendar(GetCalendarRequest(start=start, end=end))
    return [c.date for c in cal]


def main(  # pragma: no cover
    argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
) -> int:
    from alpaca.trading.client import TradingClient

    from bot.config import ConfigError, Settings, load_env_file
    from bot.data import BarCache, load_daily_bars, make_data_client
    from bot.strategies import strategy_config
    from bot.strategies.kronos_rank import KronosConfig
    from bot.universe import universe_for

    ap = argparse.ArgumentParser(prog="python -m bot.kronos", description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--symbols", help="comma-separated subset of the universe")
    ap.add_argument("--universe")
    ap.add_argument("--out", help="forecast cache (default: KRONOS_FORECASTS or the strategy's)")
    ap.add_argument("--cache", default="data/bars")
    args = ap.parse_args(argv)

    env = env if env is not None else {**load_env_file(Path(".env")), **os.environ}
    try:
        settings = Settings.from_env(env)
        data = make_data_client(settings)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cfg = strategy_config("kronos_rank", settings) or KronosConfig()
    out = args.out or cfg.forecast_path
    trading = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)
    members = universe_for(args.universe or settings.universe_name)
    symbols = args.symbols.split(",") if args.symbols else sorted(members)
    days = _trading_days(trading, args.start, args.end)
    calendar = _trading_days(trading, args.start, args.end + timedelta(days=30))

    def future_days(day: date, n: int) -> pd.DatetimeIndex:
        after = [d for d in calendar if d > day][:n]
        if len(after) < n:
            raise ValueError(f"calendar too short after {day}")
        return pd.DatetimeIndex(pd.to_datetime(after)).tz_localize("America/New_York")

    first = args.start - timedelta(days=int(cfg.lookback * 1.6) + 10)
    loaded = load_daily_bars(data, symbols, first, args.end, BarCache(args.cache))
    have = read_forecasts(out, cfg.model, cfg.seed)
    predict = kronos_predictor(
        Path(env.get("KRONOS_SRC", str(DEFAULT_SRC))), cfg.model, DEFAULT_TOKENIZER
    )
    total = 0
    for symbol, bars in sorted(loaded.bars.items()):
        todo = [d for d in days if (symbol, d) not in have.index]
        rows = forecast_symbol(
            symbol,
            bars,
            todo,
            predict,
            future_days,
            model=cfg.model,
            seed=cfg.seed,
            lookback=cfg.lookback,
            horizon=cfg.horizon,
            samples=cfg.samples,
        )
        total += append_forecasts(out, rows)
        print(f"{symbol}: {len(rows)} forecasts", flush=True)
    print(f"wrote {total} forecasts to {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
