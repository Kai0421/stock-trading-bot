# Author: Kai0421
"""Analyst desk: a free, rule-based take on TradingAgents (docs/13-analyst-desk.md).

Five analysts read one source each (price trend, market regime, SEC fundamentals, insider
filings, news headlines) and a judge combines them into Buy / Overweight / Hold /
Underweight / Sell, with the bull case, the bear case and any risk caps.

Research only: nothing here places, changes or cancels orders, and the rating is not used
by the engine. It is deterministic and point in time, so it can be backtested before any
use in trading (which would need the probability gate and a memory/decisions.md entry).

    uv run python -m bot.desk AAPL                 # premarket view for today
    uv run python -m bot.desk AAPL --date 2026-09-15 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd

from bot.desk.analysts import fundamentals, insiders, market, news, technical
from bot.desk.facts import AnnualFacts, annual_facts
from bot.desk.judge import Decision, decide

if TYPE_CHECKING:
    from bot.config import Settings

ET = ZoneInfo("America/New_York")
BAR_LOOKBACK_DAYS = 550  # calendar days: comfortably more than 253 sessions
NEWS_LOOKBACK_BDAYS = 3

__all__ = ["DeskInputs", "Decision", "analyze", "gather", "main"]


@dataclass(frozen=True)
class DeskInputs:
    bars: pd.DataFrame | None
    spy_bars: pd.DataFrame | None
    facts: AnnualFacts | None
    insider_flags: Mapping[str, bool] | None
    headlines: Sequence[str] | None


def analyze(symbol: str, as_of: date, inputs: DeskInputs) -> Decision:
    """The desk's rating for ``symbol`` before the open on ``as_of``."""
    return decide(
        [
            technical(inputs.bars, as_of),
            market(inputs.spy_bars, as_of),
            fundamentals(inputs.facts),
            insiders(inputs.insider_flags),
            news(inputs.headlines),
        ]
    )


def gather(symbol: str, as_of: date, settings: Settings) -> tuple[DeskInputs, list[str]]:
    """Live inputs from Alpaca and EDGAR. Each source that fails is left out (its analyst
    becomes unavailable) and its error is returned."""
    from bot.altdata import EDGAR_FEATURES, LiveSource, compute_features
    from bot.altdata.store import AltStore
    from bot.data import BarCache, load_daily_bars, make_data_client
    from bot.journal import Journal
    from bot.universe import BENCHMARK

    errors: list[str] = []
    bars = spy = None
    try:
        client = make_data_client(settings)
        loaded = load_daily_bars(
            client,
            [symbol, BENCHMARK],
            as_of - timedelta(days=BAR_LOOKBACK_DAYS),
            as_of - timedelta(days=1),
            BarCache(),
        )
        bars, spy = loaded.bars.get(symbol), loaded.bars.get(BENCHMARK)
        errors += [f"bars {s}: {why}" for s, why in loaded.skipped.items()]
    except Exception as exc:
        errors.append(f"bars: {exc}")

    store = AltStore(Journal(settings.db_path))
    facts = flags = None
    if settings.sec_user_agent:
        try:
            from bot.altdata.edgar import EdgarClient

            edgar = EdgarClient(settings.sec_user_agent)
            cik = edgar.ticker_ciks().get(symbol)
            if cik is None:
                errors.append(f"EDGAR: no company CIK for {symbol}")
            else:
                facts = annual_facts(edgar.company_facts(cik), as_of)
                LiveSource(store, edgar, None, EDGAR_FEATURES).refresh(symbol, as_of)
                flags = {
                    k: v
                    for k, v in compute_features(store, symbol, as_of).items()
                    if k != "news_shock_veto"
                }
        except Exception as exc:
            errors.append(f"EDGAR: {exc}")
    else:
        errors.append("EDGAR: SEC_USER_AGENT is not set")

    headlines = None
    if settings.alpaca_api_key and settings.alpaca_secret_key:
        try:
            from alpaca.data.historical.news import NewsClient

            client = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
            LiveSource(store, None, client, frozenset({"news_shock_veto"})).refresh(
                symbol, as_of, lookback_days=7
            )
            start_day = (pd.Timestamp(as_of) - pd.offsets.BDay(NEWS_LOOKBACK_BDAYS)).date()
            rows = store.news(
                symbol,
                datetime.combine(start_day, time(0), tzinfo=ET),
                datetime.combine(as_of, time(9, 0), tzinfo=ET),
            )
            headlines = [r["headline"] for r in rows]
        except Exception as exc:
            errors.append(f"news: {exc}")
    return DeskInputs(bars, spy, facts, flags, headlines), errors


def main(argv: Sequence[str] | None = None) -> int:
    from bot.config import ConfigError, Settings, load_env_file

    ap = argparse.ArgumentParser(prog="python -m bot.desk", description=__doc__.split("\n")[0])
    ap.add_argument("symbol")
    ap.add_argument("--date", help="as-of trading day, YYYY-MM-DD (default: today, ET)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    ap.add_argument("--out", help="also write the report to this file")
    args = ap.parse_args(argv)
    try:
        as_of = date.fromisoformat(args.date) if args.date else datetime.now(ET).date()
    except ValueError:
        print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2
    symbol = args.symbol.upper()
    try:
        settings = Settings.from_env({**load_env_file(Path(".env")), **os.environ})
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    inputs, errors = gather(symbol, as_of, settings)
    for e in errors:
        print(f"warning: {e}", file=sys.stderr)
    decision = analyze(symbol, as_of, inputs)
    if args.json:
        text = json.dumps({"symbol": symbol, "as_of": as_of.isoformat(), **decision.to_dict()},
                          indent=2)  # fmt: skip
    else:
        text = decision.to_markdown(symbol, as_of)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    return 0
