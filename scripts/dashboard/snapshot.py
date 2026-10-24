# Author: Kai0421
"""Read-only snapshot for the bot dashboard. Run from the repo root:
    uv run python scripts/dashboard/snapshot.py
Reads the Alpaca PAPER account (GET requests only) plus data/bot.db and memory/state.json,
and writes data/dashboard/snapshot.json. It never places, changes or cancels orders.
"""

import json
import os
import sqlite3
import ssl
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://paper-api.alpaca.markets"
OUT = Path("data/dashboard/snapshot.json")


def env():
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ctx():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def get(path):
    req = urllib.request.Request(
        BASE + path,
        headers={
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx()) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": f"{path}: {e}"}


def db():
    out = {}
    p = Path("data/bot.db")
    if not p.exists():
        return out
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    for t in [
        "trades",
        "positions",
        "orders",
        "equity",
        "signals",
        "plans",
        "shadow_trades",
        "runs",
        "flags",
    ]:
        try:
            out[t] = [dict(r) for r in c.execute(f"select * from {t}")]
        except sqlite3.Error as e:
            out[t] = {"error": str(e)}
    return out


def build():
    env()
    acct = get("/v2/account")
    keep = [
        "equity",
        "last_equity",
        "cash",
        "non_marginable_buying_power",
        "portfolio_value",
        "long_market_value",
        "status",
        "created_at",
        "currency",
    ]
    snap = {
        "generated_at": datetime.now(UTC).isoformat(),
        "env": {
            k: os.environ.get(k)
            for k in [
                "PAPER",
                "DRY_RUN",
                "STRATEGY",
                "PAPER_TRIAL",
                "TRADING_MODE",
                "GATE_MODE",
                "RISK_PER_TRADE",
                "MAX_POSITION_PCT",
                "MAX_OPEN_POSITIONS",
            ]
        },
        "account": {k: acct.get(k) for k in keep} if "error" not in acct else acct,
        "positions": get("/v2/positions"),
        "orders": get("/v2/orders?status=all&limit=500&direction=desc&nested=true"),
        "fills": get("/v2/account/activities/FILL?page_size=100"),
        "history": get("/v2/account/portfolio/history?period=1A&timeframe=1D"),
        "history_intraday": get(
            "/v2/account/portfolio/history?period=1W&timeframe=1H&extended_hours=false"
        ),
        "bot": db(),
        "state": json.loads(Path("memory/state.json").read_text())
        if Path("memory/state.json").exists()
        else None,
    }
    return json.loads(json.dumps(snap, default=str))


def main():
    snap = build()
    OUT.write_text(json.dumps(snap, indent=1))
    a = snap["account"]
    print(
        "wrote",
        OUT,
        "| equity",
        a.get("equity"),
        "| positions",
        len(snap["positions"]) if isinstance(snap["positions"], list) else snap["positions"],
        "| orders",
        len(snap["orders"]) if isinstance(snap["orders"], list) else snap["orders"],
        "| bot trades",
        len(snap["bot"].get("trades", [])),
    )


if __name__ == "__main__":
    main()
