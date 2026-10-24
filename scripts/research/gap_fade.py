# Author: Kai0421
"""One-off research (read-only market data, no orders): which stocks gap up overnight and
then fade after the open? Run from the repo root:  uv run python scripts/research/gap_fade.py
Writes data/research/gap_fade_per_stock.csv
"""

import csv
import io
import os
import ssl
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import certifi
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

START, END, GAP = datetime(2021, 10, 1, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC), 0.02
OUT = Path("data/research")
SP500_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)
EXTRA = [
    "ARM",
    "ASML",
    "AZN",
    "CCEP",
    "CDW",
    "CSGP",
    "DDOG",
    "FANG",
    "GFS",
    "MELI",
    "MRVL",
    "PDD",
    "TEAM",
    "TTD",
    "WBD",
    "ZS",
    "SMCI",
    "MSTR",
    "APP",
    "COIN",
    "PLTR",
    "SHOP",
    "CRWD",
    "MDB",
    "DASH",
    "MU",
]


def env():
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def universe():
    try:
        with urllib.request.urlopen(
            SP500_URL, timeout=30, context=ssl.create_default_context(cafile=certifi.where())
        ) as r:
            rows = list(csv.DictReader(io.StringIO(r.read().decode())))
        syms = {x["Symbol"].replace("-", ".") for x in rows}
    except Exception as e:
        print("S&P list download failed, using local bars list:", e)
        syms = {p.stem for p in Path("data/bars").glob("*.csv")}
    return sorted(syms | set(EXTRA))


def fetch(client, syms):
    frames = []
    for i in range(0, len(syms), 50):
        chunk = syms[i : i + 50]
        for feed in (DataFeed.SIP, DataFeed.IEX):
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=TimeFrame.Day,
                    start=START,
                    end=END,
                    adjustment=Adjustment.SPLIT,
                    feed=feed,
                )
                frames.append(client.get_stock_bars(req).df.assign(feed=feed.value))
                print(f"{i + len(chunk)}/{len(syms)} via {feed.value}", flush=True)
                break
            except Exception as e:
                print(f"chunk {i} {feed.value} failed: {e}", flush=True)
    return pd.concat(frames)


def stats(g):
    g = g.sort_index()
    prev = g["close"].shift(1)
    on = g["open"] / prev - 1
    oc = g["close"] / g["open"] - 1
    up = on > GAP
    gu = g[up]
    giveback = (gu["open"] - gu["close"]) / (gu["open"] - prev[up])
    return {
        "feed": g["feed"].iloc[0],
        "first_day": str(g.index[0].date()),
        "days": int(on.notna().sum()),
        "avg_overnight_pct": on.mean() * 100,
        "avg_intraday_pct": oc.mean() * 100,
        "total_overnight_pct": ((1 + on.fillna(0)).prod() - 1) * 100,
        "total_intraday_pct": ((1 + oc).prod() - 1) * 100,
        "gap_ups": int(up.sum()),
        "avg_gap_pct": on[up].mean() * 100,
        "gapup_avg_open_to_close_pct": oc[up].mean() * 100,
        "gapup_median_open_to_close_pct": oc[up].median() * 100,
        "gapup_fade_rate_pct": (oc[up] < 0).mean() * 100,
        "gapup_median_giveback_pct": giveback.median() * 100,
        "gapup_gap_filled_pct": (gu["low"] <= prev[up]).mean() * 100,
        "gapup_close_below_prev_close_pct": (gu["close"] < prev[up]).mean() * 100,
        "all_days_fade_rate_pct": (oc < 0).mean() * 100,
    }


def main():
    env()
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )
    df = fetch(client, universe())
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "gap_fade_daily_bars.csv.gz")
    rows = [
        {"symbol": s, **stats(g.droplevel("symbol"))}
        for s, g in df.groupby(level="symbol")
        if len(g) >= 250
    ]
    res = pd.DataFrame(rows).round(3)
    res.to_csv(OUT / "gap_fade_per_stock.csv", index=False)
    print(len(res), "stocks written to", OUT / "gap_fade_per_stock.csv")
    print(res[res.symbol == "MU"].T.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
