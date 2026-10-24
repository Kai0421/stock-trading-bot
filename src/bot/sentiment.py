# Author: Kai0421
"""Stocktwits sentiment shadow log (research only): no orders, nothing in the bot imports it.

Stocktwits blocks the bot's own requests (403), so a scheduled Claude job reads each stock's
"symbol pulse" through the Stocktwits connector, saves the raw payloads to a JSON file and
runs ``python -m bot.sentiment ingest``. This module then:

- turns each payload into a ``Snapshot`` (community score 0-100, message volume 0-100, price,
  and a net score from the recent posts users tagged Bullish or Bearish) and appends it to
  ``data/sentiment/snapshots.jsonl``. The price is stored so a later offline test can join the
  snapshots to forward returns and ask whether the signal predicts anything;
- classifies each snapshot with fixed thresholds (``Thresholds``: hypotheses, not tuned):
  ``buy_watch`` for a stock we do not hold, ``sell_warning`` for one we do;
- keeps a small watchlist that forgets a stock after 5 trading days or 2 neutral days in a
  row, never while it is held.

It decides nothing about orders. A buy would need the 60% gate (AGENTS.md) on the logged
history first; a sell warning is information for the owner until a mode exists for it.

    uv run python -m bot.sentiment ingest raw.json --held AMD,NVDA
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
DEFAULT_DIR = Path("data/sentiment")
MAX_POST_WEIGHT = 3.0  # a viral post counts at most 3 ordinary ones
MAX_TICKERS_PER_POST = 3  # posts tagging more are ticker spam
WATCH_DAYS = 5  # trading days before a watchlist entry is forgotten
NEUTRAL_DAYS = 2  # neutral days in a row before it is forgotten
NEUTRAL_BAND = (40, 60)
FULL_FROM = time(15, 30)  # ET: the whole-universe collection runs once, from here
HELD_FROM, HELD_EVERY_MIN = time(10, 0), 30  # ET: held stocks are re-read about this often


@dataclass(frozen=True)
class Thresholds:
    """Hypotheses to be tested on the logged history, not tuned values."""

    buy_score: float = 75
    buy_volume: float = 60
    min_tagged: int = 3
    sell_score: float = 30
    sell_net: float = -0.2
    sell_volume: float = 50


DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Snapshot:
    ts: str
    symbol: str
    price: float | None
    score: float
    label: str = ""
    bull_pct: float | None = None
    bear_pct: float | None = None
    delta: float | None = None
    volume_score: float | None = None
    post_net: float | None = None
    post_tagged: int = 0
    held: bool = False


@dataclass(frozen=True)
class Alert:
    symbol: str
    kind: str
    score: float
    volume_score: float | None
    post_net: float | None
    price: float | None


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def post_weight(post: Mapping[str, Any]) -> float:
    engaged = sum(float(post.get(k) or 0) for k in ("likes", "reshares", "replies"))
    return min(MAX_POST_WEIGHT, 1 + math.log1p(engaged))


def score_posts(
    posts: Iterable[Mapping[str, Any]], symbol: str, now: datetime, window_minutes: int = 30
) -> tuple[float | None, int]:
    """Net sentiment (-1..1) of recent posts users tagged Bullish or Bearish, and how many.

    Skips untagged posts, posts outside the window, posts tagging more than three tickers,
    and every vote after a user's first; weights by engagement up to ``MAX_POST_WEIGHT``.
    """
    cutoff = _utc(now) - timedelta(minutes=window_minutes)
    seen: set[Any] = set()
    total = weight_sum = 0.0
    tagged = 0
    for p in posts:
        tag = str(p.get("sentiment") or "").lower()
        if tag not in ("bullish", "bearish"):
            continue
        syms = p.get("symbols") or []
        if symbol not in syms or len(syms) > MAX_TICKERS_PER_POST:
            continue
        try:
            at = datetime.fromisoformat(str(p["created_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if _utc(at) < cutoff:
            continue
        user = (p.get("user") or {}).get("id", p.get("id"))
        if user in seen:
            continue
        seen.add(user)
        w = post_weight(p)
        total += w if tag == "bullish" else -w
        weight_sum += w
        tagged += 1
    return (total / weight_sum, tagged) if tagged else (None, 0)


def _first(d: Mapping[str, Any], *keys: str) -> Any:
    """First present key: get_symbol_pulse and get_sentiment name the same fields differently."""
    return next((d[k] for k in keys if d.get(k) is not None), None)


def _volume_now(mv: Mapping[str, Any]) -> float | None:
    """Volume score from a pulse (``score``) or get_message_volume (``series`` at ``now``)."""
    if mv.get("score") is not None:
        return float(mv["score"])
    for row in mv.get("series") or []:
        if row.get("timeframe") == "now" and row.get("normalized_value") is not None:
            return float(row["normalized_value"])
    return None


def snapshot_from_pulse(payload: Mapping[str, Any], now: datetime, held: bool = False) -> Snapshot:
    symbol = str(payload.get("symbol") or "").upper()
    senti = payload.get("sentiment") or {}
    if not symbol or senti.get("score") is None:
        raise ValueError(f"pulse payload for {symbol or '?'} has no sentiment score")
    vol = _volume_now(payload.get("message_volume") or {})
    net, tagged = score_posts(payload.get("posts") or [], symbol, now)
    return Snapshot(
        ts=_utc(now).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        symbol=symbol,
        price=payload.get("price"),
        score=float(senti["score"]),
        label=str(senti.get("label") or ""),
        bull_pct=_first(senti, "bull_pct", "bullish_pct"),
        bear_pct=_first(senti, "bear_pct", "bearish_pct"),
        delta=_first(senti, "delta", "bullish_delta"),
        volume_score=None if vol is None else float(vol),
        post_net=net,
        post_tagged=tagged,
        held=held,
    )


def classify(s: Snapshot, t: Thresholds = DEFAULT_THRESHOLDS) -> str | None:
    """``buy_watch`` (not held), ``sell_warning`` (held) or None."""
    if s.held:
        if (
            s.score <= t.sell_score
            and s.post_net is not None
            and s.post_net <= t.sell_net
            and (s.volume_score or 0) >= t.sell_volume
        ):
            return "sell_warning"
        return None
    if (
        s.score >= t.buy_score
        and (s.volume_score or 0) >= t.buy_volume
        and s.post_tagged >= t.min_tagged
        and s.post_net is not None
        and s.post_net > 0
    ):
        return "buy_watch"
    return None


def update_watchlist(
    entries: Mapping[str, Mapping[str, Any]],
    snaps: Sequence[Snapshot],
    today: date,
    held: set[str],
    t: Thresholds = DEFAULT_THRESHOLDS,
) -> dict[str, dict[str, Any]]:
    """Add signalling stocks, forget stale ones. Held stocks are never forgotten."""
    out = {k: dict(v) for k, v in entries.items()}
    for sym in list(out):  # business days ignore exchange holidays: close enough here
        age = int(np.busday_count(date.fromisoformat(out[sym]["first_seen"]), today))
        if age >= WATCH_DAYS and sym not in held:
            del out[sym]
    for s in snaps:
        kind = classify(s, t)
        if kind:
            e = out.setdefault(s.symbol, {"first_seen": today.isoformat()})
            e.update(kind=kind, last_seen=today.isoformat(), neutral_days=0, neutral_date="")
        elif s.symbol in out and s.symbol not in held:
            e = out[s.symbol]
            if NEUTRAL_BAND[0] <= s.score <= NEUTRAL_BAND[1]:
                if e.get("neutral_date") != today.isoformat():
                    e["neutral_days"] = int(e.get("neutral_days", 0)) + 1
                    e["neutral_date"] = today.isoformat()
                if e["neutral_days"] >= NEUTRAL_DAYS:
                    del out[s.symbol]
            else:
                e["neutral_days"], e["neutral_date"] = 0, ""
    return out


def latest_fresh(
    snaps: Iterable[Snapshot], symbols: Iterable[str], now: datetime, max_age_minutes: int
) -> dict[str, Snapshot]:
    """Newest snapshot per wanted symbol that is at most ``max_age_minutes`` old.

    Fails closed: unreadable or future timestamps are ignored, so a stale or broken file
    yields nothing rather than an old signal.
    """
    wanted, now = set(symbols), _utc(now)
    oldest = now - timedelta(minutes=max_age_minutes)
    out: dict[str, tuple[datetime, Snapshot]] = {}
    for s in snaps:
        if s.symbol not in wanted:
            continue
        try:
            at = _utc(datetime.fromisoformat(s.ts.replace("Z", "+00:00")))
        except ValueError:
            continue
        if not oldest <= at <= now:
            continue
        if s.symbol not in out or at >= out[s.symbol][0]:
            out[s.symbol] = (at, s)
    return {sym: snap for sym, (_, snap) in out.items()}


def due_mode(now_et: datetime, session: bool, held: set[str], state: Mapping[str, Any]) -> str:
    """Which collection is due now: ``full`` (once a session day from 15:30 ET), ``held``
    (held stocks every ~30 minutes from 10:00 ET) or ``none``. Pure; the caller supplies state."""
    if not session:
        return "none"
    today, t = now_et.date().isoformat(), now_et.timetz().replace(tzinfo=None)
    if t >= FULL_FROM:
        return "none" if state.get("last_full_day") == today else "full"
    if not held or t < HELD_FROM:
        return "none"
    try:
        last = datetime.fromisoformat(str(state.get("last_held_ts", "")).replace("Z", "+00:00"))
        if _utc(now_et) - _utc(last) < timedelta(minutes=HELD_EVERY_MIN):
            return "none"
    except ValueError:
        pass  # never run, or a garbled state file: run
    return "held"


def read_state(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: str | Path, state: Mapping[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(state), indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def append_snapshots(path: str | Path, snaps: Iterable[Snapshot]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for s in snaps:
            f.write(json.dumps(asdict(s), separators=(",", ":")) + "\n")


def read_snapshots(path: str | Path) -> list[Snapshot]:
    p = Path(path)
    if not p.exists():
        return []
    names = {f.name for f in fields(Snapshot)}
    out: list[Snapshot] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(Snapshot(**{k: v for k, v in json.loads(line).items() if k in names}))
        except (json.JSONDecodeError, TypeError):
            continue  # a torn last line from an interrupted write
    return out


def ingest(
    raw: str | Path,
    out_dir: str | Path = DEFAULT_DIR,
    held: set[str] | None = None,
    now: datetime | None = None,
    t: Thresholds = DEFAULT_THRESHOLDS,
    mode: str | None = None,
) -> list[Alert]:
    """Store one run's pulse payloads, update the watchlist, return the alerts.

    ``mode`` (``full`` or ``held``) records the run in ``state.json`` for ``due_mode``."""
    held = {h.upper() for h in (held or set())}
    now = now or datetime.now(UTC)
    out = Path(out_dir)
    payloads = json.loads(Path(raw).read_text(encoding="utf-8"))
    snaps: list[Snapshot] = []
    for p in payloads:
        try:
            snaps.append(snapshot_from_pulse(p, now, held=str(p.get("symbol", "")).upper() in held))
        except (ValueError, AttributeError) as exc:
            print(f"skipped: {exc}", file=sys.stderr)
    append_snapshots(out / "snapshots.jsonl", snaps)
    wl_path = out / "watchlist.json"
    try:
        current = json.loads(wl_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        current = {}
    today = _utc(now).astimezone(ET).date()
    wl = update_watchlist(current, snaps, today, held, t)
    tmp = wl_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(wl, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(wl_path)
    if mode:
        state = read_state(out / "state.json")
        state["last_held_ts"] = _utc(now).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if mode == "full":
            state["last_full_day"] = today.isoformat()
        write_state(out / "state.json", state)
    return [
        Alert(s.symbol, k, s.score, s.volume_score, s.post_net, s.price)
        for s in snaps
        if (k := classify(s, t))
    ]


def _print_due(out: Path) -> int:
    """Scheduler hook: reads only the local journal and calendar, never the network."""
    from bot.calendar import MarketCalendar
    from bot.config import Settings, load_env_file
    from bot.journal import Journal

    env_file = Path(".env")
    settings = Settings.from_env(load_env_file(env_file) if env_file.exists() else {})
    now = datetime.now(ET)
    held = set(Journal(settings.db_path).positions())
    mode = due_mode(
        now, MarketCalendar().is_trading_day(now.date()), held, read_state(out / "state.json")
    )
    print(mode, ",".join(sorted(held)) or "-")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="store a run of raw pulse payloads")
    ing.add_argument("raw", help="JSON list of get_symbol_pulse payloads")
    ing.add_argument("--held", default="", help="comma-separated symbols we hold")
    ing.add_argument("--out", default=str(DEFAULT_DIR))
    ing.add_argument("--mode", choices=("full", "held"), help="record this run for `due`")
    due = sub.add_parser("due", help="print '<full|held|none> <held symbols>' for the scheduler")
    due.add_argument("--out", default=str(DEFAULT_DIR))
    args = ap.parse_args()
    if args.cmd == "due":
        return _print_due(Path(args.out))
    held = {h.strip().upper() for h in args.held.split(",") if h.strip()}
    alerts = ingest(args.raw, args.out, held, mode=args.mode)
    for a in alerts:
        print(
            f"{a.kind}: {a.symbol} score {a.score:.0f} volume {a.volume_score} "
            f"posts {a.post_net} price {a.price}"
        )
    print(f"{len(alerts)} alert(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
