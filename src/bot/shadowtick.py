# Author: Kai0421
"""Shadow tick: what would the decision logic do for the positions held right now?

Read-only. A snapshot of the broker's positions and open orders (reads only; every write is
blocked) plus a copy of the journal go through the decision layer's functions for open
positions: ``protect_actions``, ``exit_intents``, ``stop_changes`` and ``entries_to_cancel``.
Running the same snapshot through two releases and comparing the answers shows whether an
upgrade would treat today's open trades differently (docs/24-hot-upgrade.md).

This file imports only ``bot.broker``, ``bot.decide``, ``bot.journal`` and ``bot.config``, which
exist in every release, so the new release's copy can load the *old* release's decision layer
(``scripts/shadow_tick.py --code <release>``). Nothing in the order path imports it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bot import decide
from bot.broker import Broker, BrokerOrder, BrokerPosition
from bot.config import Settings, load_env_file
from bot.journal import Journal, PositionRow

ET = ZoneInfo("America/New_York")
READ_METHODS = frozenset(
    {"account", "clock", "positions", "open_orders", "closed_orders", "order_by_client_id"}
)


class WriteBlocked(RuntimeError):
    """The shadow tick tried something other than a read."""


class ReadOnlyBroker:
    """Wraps a broker and lets only reads through; every other call raises."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in READ_METHODS:
            return getattr(self._inner, name)
        raise WriteBlocked(f"the shadow tick is read-only: {name} is not allowed")


@dataclass(frozen=True)
class Snapshot:
    positions: dict[str, BrokerPosition]
    orders: list[BrokerOrder]


def snapshot(broker: ReadOnlyBroker) -> Snapshot:
    return Snapshot(dict(broker.positions()), list(broker.open_orders()))


def _order_to_json(o: BrokerOrder) -> dict[str, Any]:
    d = asdict(o)
    d["legs"] = [_order_to_json(leg) for leg in o.legs]
    for k in ("submitted_at", "filled_at"):
        d[k] = getattr(o, k).isoformat() if getattr(o, k) else None
    return d


def _order_from_json(d: Mapping[str, Any]) -> BrokerOrder:
    fields = dict(d)
    fields["legs"] = tuple(_order_from_json(x) for x in d["legs"])
    for k in ("submitted_at", "filled_at"):
        fields[k] = datetime.fromisoformat(d[k]) if d[k] else None
    return BrokerOrder(**fields)


def snapshot_to_json(s: Snapshot) -> dict[str, Any]:
    return {
        "positions": [asdict(p) for p in s.positions.values()],
        "orders": [_order_to_json(o) for o in s.orders],
    }


def snapshot_from_json(d: Mapping[str, Any]) -> Snapshot:
    positions = {p["symbol"]: BrokerPosition(**p) for p in d["positions"]}
    return Snapshot(positions, [_order_from_json(o) for o in d["orders"]])


# ------------------------------------------------------------------ intents


def compute_intents(
    positions: Mapping[str, BrokerPosition],
    orders: Sequence[BrokerOrder],
    rows: Mapping[str, PositionRow],
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """What the decision layer would do for open positions and orders right now."""
    protect = [
        {"symbol": a.symbol, "qty": a.qty, "stop": a.stop, "sell": a.sell, "cid": a.cid}
        for a in decide.protect_actions(positions, orders, rows, now)
    ]
    exits = [
        {"symbol": e.row.symbol, "kind": e.kind, "qty": e.qty, "full": e.full, "cid": e.cid}
        for e in decide.exit_intents(rows, positions, now.date())
    ]
    stops = [
        {"order": c.order_id, "symbol": c.symbol, "stop_price": c.stop_price, "qty": c.qty}
        for c in decide.stop_changes(rows, orders, positions)
    ]
    cancels = [{"order": o.id, "symbol": o.symbol} for o in decide.entries_to_cancel(orders, now)]
    key = lambda d: json.dumps(d, sort_keys=True)  # noqa: E731
    return {
        "protect": sorted(protect, key=key),
        "exits": sorted(exits, key=key),
        "stop_changes": sorted(stops, key=key),
        "cancel_entries": sorted(cancels, key=key),
    }


_LABEL = {
    "protect": "adding a missing stop",
    "exits": "selling",
    "stop_changes": "moving a stop",
    "cancel_entries": "cancelling a buy order",
}


def _plain(item: Mapping[str, Any] | None) -> str:
    if item is None:
        return "nothing"
    return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in item.items() if k != "order")


def diff_intents(
    current: Mapping[str, Sequence[Mapping[str, Any]]],
    new: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[str]:
    """Plain-English differences between two releases' plans (empty means identical)."""
    out = []
    for cat, label in _LABEL.items():
        a = {i.get("symbol", "") + i.get("order", ""): i for i in current.get(cat, [])}
        b = {i.get("symbol", "") + i.get("order", ""): i for i in new.get(cat, [])}
        for k in sorted(a.keys() | b.keys()):
            if a.get(k) != b.get(k):
                who = (a.get(k) or b.get(k) or {}).get("symbol", k)
                out.append(
                    f"{who}: for {label}, the current version plans {_plain(a.get(k))} but "
                    f"the new version plans {_plain(b.get(k))}"
                )
    return out


# ------------------------------------------------------------------ who may upgrade when


def flat_blockers(positions: Mapping[str, Any], orders: Sequence[BrokerOrder]) -> list[str]:
    out = []
    if positions:
        out.append(f"the account holds {', '.join(sorted(positions))}")
    if orders:
        out.append(f"{len(orders)} order(s) are still open at the broker")
    return out


def hot_blockers(
    positions: Mapping[str, BrokerPosition],
    orders: Sequence[BrokerOrder],
    rows: Mapping[str, PositionRow],
    now: datetime,
) -> list[str]:
    """Why an upgrade while holding is not safe right now (empty means it may go ahead)."""
    out = []
    for a in decide.protect_actions(positions, orders, rows, now):
        out.append(f"{a.symbol}: not all of the shares are covered by a stop at the broker")
    out += [
        f"{s}: held at the broker but missing from the bot's journal"
        for s in sorted(positions)
        if s not in rows
    ]
    out += [
        f"{s}: the journal says it is held but the broker shows nothing yet"
        for s in sorted(rows)
        if s not in positions
    ]
    out += [
        f"{o.symbol}: a buy order is still working"
        for o in decide.flatten_orders(list(orders))
        if o.side == "buy"
    ]
    return out


# ------------------------------------------------------------------ command line


def _now(text: str | None) -> datetime:
    return datetime.fromisoformat(text) if text else datetime.now(ET)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shadow_tick")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="read the broker's positions and open orders (read-only)")
    s.add_argument("--out", type=Path, required=True)
    i = sub.add_parser("intents", help="what this code would do for the snapshot")
    i.add_argument("--db", type=Path, required=True, help="a COPY of data/bot.db")
    i.add_argument("--snapshot", type=Path, required=True)
    i.add_argument("--now")
    i.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("diff", help="compare two intents files; exit 1 if they differ")
    d.add_argument("current", type=Path)
    d.add_argument("new", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "snapshot":
        settings = Settings.from_env({**load_env_file(Path(".env")), **os.environ})
        snap = snapshot(ReadOnlyBroker(Broker.from_settings(settings)))
        args.out.write_text(json.dumps(snapshot_to_json(snap)), encoding="utf-8")
        print(f"snapshot: {len(snap.positions)} position(s), {len(snap.orders)} open order(s)")
        return 0
    if args.cmd == "intents":
        snap = snapshot_from_json(json.loads(args.snapshot.read_text(encoding="utf-8")))
        rows = Journal(args.db).positions()
        out = compute_intents(snap.positions, snap.orders, rows, _now(args.now))
        args.out.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
        return 0
    a = json.loads(args.current.read_text(encoding="utf-8"))
    b = json.loads(args.new.read_text(encoding="utf-8"))
    lines = diff_intents(a, b)
    print("\n".join(lines) if lines else "identical: the new version would do exactly the same")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
